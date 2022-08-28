# Copyright (c) OpenMMLab. All rights reserved.
from abc import ABCMeta, abstractmethod

import torch
import torch.nn as nn
from mmcv.runner import BaseModule, auto_fp16, force_fp32

from mmseg.core import build_pixel_sampler
from mmseg.ops import resize
from ..builder import build_loss
from ..losses import accuracy
import torch.nn.functional as F

from pyro.distributions.transforms.planar import Planar
from pyro.distributions.transforms.radial import Radial
from pyro.distributions.transforms.affine_autoregressive import affine_autoregressive
import torch.distributions as tdist

"test w/: python tools/train.py configs/deeplabv3/deeplabv3_r50-d8_720x720_70e_cityscapes_nf_bll.py --experiment-tag 'TEST'"


class NfBllBaseDecodeHead(BaseModule, metaclass=ABCMeta):
    """Base class for NFBLLBaseDecodeHead.

    Args:
        in_channels (int|Sequence[int]): Input channels.
        channels (int): Channels after modules, before conv_seg.
        num_classes (int): Number of classes.
        dropout_ratio (float): Ratio of dropout layer. Default: 0.1.
        conv_cfg (dict|None): Config of conv layers. Default: None.
        norm_cfg (dict|None): Config of norm layers. Default: None.
        act_cfg (dict): Config of activation layers.
            Default: dict(type='ReLU')
        in_index (int|Sequence[int]): Input feature index. Default: -1
        input_transform (str|None): Transformation type of input features.
            Options: 'resize_concat', 'multiple_select', None.
            'resize_concat': Multiple feature maps will be resize to the
                same size as first one and than concat together.
                Usually used in FCN head of HRNet.
            'multiple_select': Multiple feature maps will be bundle into
                a list and passed into decode head.
            None: Only one select feature map is allowed.
            Default: None.
        loss_decode (dict | Sequence[dict]): Config of decode loss.
            The `loss_name` is property of corresponding loss function which
            could be shown in training log. If you want this loss
            item to be included into the backward graph, `loss_` must be the
            prefix of the name. Defaults to 'loss_ce'.
             e.g. dict(type='CrossEntropyLoss'),
             [dict(type='CrossEntropyLoss', loss_name='loss_ce'),
              dict(type='DiceLoss', loss_name='loss_dice')]
            Default: dict(type='CrossEntropyLoss').
        ignore_index (int | None): The label index to be ignored. When using
            masked BCE loss, ignore_index should be set to None. Default: 255.
        sampler (dict|None): The config of segmentation map sampler.
            Default: None.
        align_corners (bool): align_corners argument of F.interpolate.
            Default: False.
        init_cfg (dict or list[dict], optional): Initialization config dict.
    """

    def __init__(self,
                 in_channels,
                 channels,
                 *,
                 num_classes,
                 dropout_ratio=0.1,
                 conv_cfg=None,
                 norm_cfg=None,
                 act_cfg=dict(type='ReLU'),
                 in_index=-1,
                 input_transform=None,
                 loss_decode=dict(
                     type='CrossEntropyLoss',
                     use_sigmoid=False,
                     loss_weight=1.0),
                 ignore_index=255,
                 sampler=None,
                 align_corners=False,
                 init_cfg=dict(
                     type='Normal', std=0.01, override=dict(name='conv_seg')),
                 flow_type='planar_flow',
                 flow_length=2,
                 nsamples_train=10):
        super(NfBllBaseDecodeHead, self).__init__(init_cfg)
        self._init_inputs(in_channels, in_index, input_transform)
        self.channels = channels
        self.num_classes = num_classes
        self.dropout_ratio = dropout_ratio
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.act_cfg = act_cfg
        self.in_index = in_index
        self.use_bags = False
        self.frozen_features = False
        self.ignore_index = ignore_index
        self.align_corners = align_corners

        if isinstance(loss_decode, dict):
            self.loss_decode = build_loss(loss_decode)
        elif isinstance(loss_decode, (list, tuple)):
            self.loss_decode = nn.ModuleList()
            for loss in loss_decode:
                self.loss_decode.append(build_loss(loss))
        else:
            raise TypeError(f'loss_decode must be a dict or sequence of dict,\
                but got {type(loss_decode)}')

        if sampler is not None:
            self.sampler = build_pixel_sampler(sampler, context=self)
        else:
            self.sampler = None

        self.conv_seg = nn.Conv2d(channels, num_classes, kernel_size=1)
        if dropout_ratio > 0:
            self.dropout = nn.Dropout2d(dropout_ratio)
        else:
            self.dropout = None
        self.fp16_enabled = False
        # Parameters for building NF
        self.latent_dim = self.conv_seg.weight.numel() + self.conv_seg.bias.numel()
        self.flow_type = flow_type
        self.flow_length = flow_length
        self.nsamples_train = nsamples_train
        self.nsamples_test = 1
        if self.flow_type in ('iaf_flow', 'planar_flow', 'radial_flow'):
            self.density_estimation = NormalizingFlowDensity(dim=self.latent_dim, flow_length=self.flow_length, flow_type=self.flow_type)
        else:
            raise NotImplementedError
        self.w_shape, self.b_shape = self.conv_seg.weight.shape, self.conv_seg.bias.shape
        self.w_numel, self.b_numel = self.conv_seg.weight.numel(), self.conv_seg.bias.numel()
        self.initial_p = torch.cat([self.conv_seg.weight.reshape(-1), self.conv_seg.bias.reshape(-1)]).detach()

    def update_z0_params(self):
        """
        To run after loading weights
        """
        self.initial_p = torch.cat([self.conv_seg.weight.reshape(-1), self.conv_seg.bias.reshape(-1)]).detach()
        self.density_estimation.mean = self.initial_p

    def conv_seg_forward(self, x, z):
        z_list = torch.split(z, 1, 0)
        output = []
        for z_ in z_list:
            z_ = z_.squeeze()
            output.append(F.conv2d(input=x, weight=z_[:self.w_numel].reshape(self.w_shape), bias=z_[-self.b_numel:].reshape(self.b_shape)))
        return torch.cat(output, dim=0)

    def extra_repr(self):
        """Extra repr."""
        s = f'input_transform={self.input_transform}, ' \
            f'ignore_index={self.ignore_index}, ' \
            f'align_corners={self.align_corners}'
        return s

    def _init_inputs(self, in_channels, in_index, input_transform):
        """Check and initialize input transforms.

        The in_channels, in_index and input_transform must match.
        Specifically, when input_transform is None, only single feature map
        will be selected. So in_channels and in_index must be of type int.
        When input_transform

        Args:
            in_channels (int|Sequence[int]): Input channels.
            in_index (int|Sequence[int]): Input feature index.
            input_transform (str|None): Transformation type of input features.
                Options: 'resize_concat', 'multiple_select', None.
                'resize_concat': Multiple feature maps will be resize to the
                    same size as first one and than concat together.
                    Usually used in FCN head of HRNet.
                'multiple_select': Multiple feature maps will be bundle into
                    a list and passed into decode head.
                None: Only one select feature map is allowed.
        """

        if input_transform is not None:
            assert input_transform in ['resize_concat', 'multiple_select']
        self.input_transform = input_transform
        self.in_index = in_index
        if input_transform is not None:
            assert isinstance(in_channels, (list, tuple))
            assert isinstance(in_index, (list, tuple))
            assert len(in_channels) == len(in_index)
            if input_transform == 'resize_concat':
                self.in_channels = sum(in_channels)
            else:
                self.in_channels = in_channels
        else:
            assert isinstance(in_channels, int)
            assert isinstance(in_index, int)
            self.in_channels = in_channels

    def _transform_inputs(self, inputs):
        """Transform inputs for decoder.

        Args:
            inputs (list[Tensor]): List of multi-level img features.

        Returns:
            Tensor: The transformed inputs
        """

        if self.input_transform == 'resize_concat':
            inputs = [inputs[i] for i in self.in_index]
            upsampled_inputs = [
                resize(
                    input=x,
                    size=inputs[0].shape[2:],
                    mode='bilinear',
                    align_corners=self.align_corners) for x in inputs
            ]
            inputs = torch.cat(upsampled_inputs, dim=1)
        elif self.input_transform == 'multiple_select':
            inputs = [inputs[i] for i in self.in_index]
        else:
            inputs = inputs[self.in_index]

        return inputs

    @auto_fp16()
    @abstractmethod
    def forward(self, inputs):
        """Placeholder of forward function."""
        pass

    def forward_train(self, inputs, img_metas, gt_semantic_seg, train_cfg):
        """Forward function for training.
        Args:
            inputs (list[Tensor]): List of multi-level img features.
            img_metas (list[dict]): List of image info dict where each dict
                has: 'img_shape', 'scale_factor', 'flip', and may also contain
                'filename', 'ori_shape', 'pad_shape', and 'img_norm_cfg'.
                For details on the values of these keys see
                `mmseg/datasets/pipelines/formatting.py:Collect`.
            gt_semantic_seg (Tensor): Semantic segmentation masks
                used if the architecture supports semantic segmentation task.
            train_cfg (dict): The training config.

        Returns:
            dict[str, Tensor]: a dictionary of loss components
        """
        seg_logits, sum_log_jacobians = self.forward(inputs, self.nsamples_train)
        losses = self.losses(seg_logits, gt_semantic_seg, sum_log_jacobians)
        return losses

    def forward_test(self, inputs, img_metas, test_cfg):
        """Forward function for testing.

        Args:
            inputs (list[Tensor]): List of multi-level img features.
            img_metas (list[dict]): List of image info dict where each dict
                has: 'img_shape', 'scale_factor', 'flip', and may also contain
                'filename', 'ori_shape', 'pad_shape', and 'img_norm_cfg'.
                For details on the values of these keys see
                `mmseg/datasets/pipelines/formatting.py:Collect`.
            test_cfg (dict): The testing config.

        Returns:
            Tensor: Output segmentation map.
        """
        return self.forward(inputs)

    def cls_seg(self, feat, z):
        """Classify each pixel."""
        if self.dropout is not None:
            feat = self.dropout(feat)
            # Here I can add weight normalization
        output = self.conv_seg_forward(feat, z)
        return output

    @force_fp32(apply_to=('seg_logit', ))
    def losses(self, seg_logit, seg_label, sum_log_jacobians):
        """Compute segmentation loss."""
        loss = dict()
        seg_logit = resize(
            input=seg_logit,
            size=seg_label.shape[2:],
            mode='bilinear',
            align_corners=self.align_corners)
        if self.sampler is not None:
            seg_weight = self.sampler.sample(seg_logit, seg_label)
        else:
            seg_weight = None
        seg_label = seg_label.squeeze(1).repeat(sum_log_jacobians.numel(), 1, 1)

        if not isinstance(self.loss_decode, nn.ModuleList):
            losses_decode = [self.loss_decode]
        else:
            losses_decode = self.loss_decode

        loss['acc_seg'] = accuracy(seg_logit, seg_label, ignore_index=self.ignore_index)

        for loss_decode in losses_decode:
            if loss_decode.loss_name not in loss:
                loss[loss_decode.loss_name] = loss_decode(seg_logit, seg_label, ignore_index=self.ignore_index) + sum_log_jacobians.mean()
                if loss_decode.loss_name.startswith("loss_edl"):
                    # load
                    logs = loss_decode.get_logs(seg_logit,
                                                seg_label,
                                                self.ignore_index)
                    logs['mean_jacobian_logdet'] = sum_log_jacobians.mean().detach()
                    # import ipdb; ipdb.set_trace()
                    loss.update(logs)
            else:
                loss[loss_decode.loss_name] += loss_decode(
                    seg_logit,
                    seg_label,
                    weight=seg_weight,
                    ignore_index=self.ignore_index)
                if loss_decode.loss_name.startswith("loss_edl"):
                    raise NotImplementedError

        return loss


class NormalizingFlowDensity(nn.Module):
    def __init__(self, dim, flow_length, flow_type='planar_flow'):
        super(NormalizingFlowDensity, self).__init__()
        self.dim = dim
        self.flow_length = flow_length
        self.flow_type = flow_type

        # Base gaussian distribution
        self.z0_mean = torch.Tensor(torch.zeros(self.dim)).cuda()
        self.z0_cov = torch.Tensor(torch.eye(self.dim)).cuda()

        if self.flow_type == 'radial_flow':
            self.transforms = nn.Sequential(*(
                Radial(dim) for _ in range(flow_length)
            ))
        elif self.flow_type == 'planar_flow':
            self.transforms = nn.Sequential(*(
                Planar(dim) for _ in range(flow_length)
            ))
        elif self.flow_type == 'iaf_flow':
            self.transforms = nn.Sequential(*(
                affine_autoregressive(dim, hidden_dims=[128, 128]) for _ in range(flow_length)
            ))
        else:
            raise NotImplementedError

    def forward(self, z):
        sum_log_jacobians = 0
        for transform in self.transforms:
            z_next = transform(z)
            sum_log_jacobians = sum_log_jacobians + transform.log_abs_det_jacobian(z, z_next)
            z = z_next
        return z, sum_log_jacobians

    def log_prob(self, x):
        z, sum_log_jacobians = self.forward(x)
        log_prob_z = tdist.MultivariateNormal(self.z0_mean, self.z0_cov).log_prob(z)
        log_prob_x = log_prob_z + sum_log_jacobians
        return log_prob_x

    def sample_base(self, n):
        return tdist.MultivariateNormal(self.z0_mean, self.z0_cov).sample([n])

    # def latent_loss(self, log_det):
    #     """Computes KL loss.
    #     Args:
    #         mean: mean of initial model w_0.
    #         log_det: log-determinant of the Jacobian.
    #     Returns: sum of KL loss over the minibatch.
    #     PS: ignore because reduces to -log_det + cte in our case as we have fixed initial distribution
    #     """
    #     return -.5 * torch.sum(self.mean.pow(2), dim=1, keepdim=True) - log_det
