# Copyright (c) OpenMMLab. All rights reserved.
import os.path as osp
import tempfile
import warnings
import torch.nn.functional as F
import mmcv
import numpy as np
import torch
from mmcv.engine import collect_results_cpu, collect_results_gpu
from mmcv.image import tensor2imgs
from mmcv.runner import get_dist_info
import seaborn as sns; sns.set_theme()
import matplotlib.pyplot as plt
from ..utils import diss
from sklearn.metrics import confusion_matrix
import joblib
from mmcv.parallel import is_module_wrapper
from mmrazor.models.algorithms.base import BaseAlgorithm


def np2tmp(array, temp_file_name=None, tmpdir=None):
    """Save ndarray to local numpy file.

    Args:
        array (ndarray): Ndarray to save.
        temp_file_name (str): Numpy file name. If 'temp_file_name=None', this
            function will generate a file name with tempfile.NamedTemporaryFile
            to save ndarray. Default: None.
        tmpdir (str): Temporary directory to save Ndarray files. Default: None.
    Returns:
        str: The numpy file name.
    """

    if temp_file_name is None:
        temp_file_name = tempfile.NamedTemporaryFile(
            suffix='.npy', delete=False, dir=tmpdir).name
    np.save(temp_file_name, array)
    return temp_file_name


def plot_mask(mask, file):
    plt.figure()
    sns.heatmap(mask.squeeze(), xticklabels=False, yticklabels=False).get_figure().savefig(file)
    plt.cla(); plt.clf(); plt.close('all')


def plot_conf(conf, file):
    plt.figure()
    sns.heatmap(
        conf.squeeze(),
        xticklabels=False, yticklabels=False).get_figure().savefig(file)
    plt.cla(); plt.clf(); plt.close('all')


def single_gpu_test(model,
                    data_loader,
                    show=False,
                    out_dir=None,
                    efficient_test=False,
                    opacity=0.5,
                    pre_eval=False,
                    format_only=False,
                    format_args={}):
    """Test with single GPU by progressive mode.

    Args:
        model (nn.Module): Model to be tested.
        data_loader (utils.data.Dataloader): Pytorch data loader.
        show (bool): Whether show results during inference. Default: False.
        out_dir (str, optional): If specified, the results will be dumped into
            the directory to save output results.
        efficient_test (bool): Whether save the results as local numpy files to
            save CPU memory during evaluation. Mutually exclusive with
            pre_eval and format_results. Default: False.
        opacity(float): Opacity of painted segmentation map.
            Default 0.5.
            Must be in (0, 1] range.
        pre_eval (bool): Use dataset.pre_eval() function to generate
            pre_results for metric evaluation. Mutually exclusive with
            efficient_test and format_results. Default: False.
        format_only (bool): Only format result for results commit.
            Mutually exclusive with pre_eval and efficient_test.
            Default: False.
        format_args (dict): The args for format_results. Default: {}.
    Returns:
        list: list of evaluation pre-results or list of save file names.
    """
    if efficient_test:
        warnings.warn(
            'DeprecationWarning: ``efficient_test`` will be deprecated, the '
            'evaluation is CPU memory friendly with pre_eval=True')
        mmcv.mkdir_or_exist('.efficient_test')
    # when none of them is set true, return segmentation results as
    # a list of np.array.
    assert [efficient_test, pre_eval, format_only].count(True) <= 1, \
        '``efficient_test``, ``pre_eval`` and ``format_only`` are mutually ' \
        'exclusive, only one of them could be true .'

    model.eval()
    assert data_loader.batch_size == 1, "TEST SCRIPT ONLY WORKS WITH BATCH SIZE=1"
    results = []
    dataset = data_loader.dataset
    prog_bar = mmcv.ProgressBar(len(dataset))
    # The pipeline about how the data_loader retrieval samples from dataset:
    # sampler -> batch_sampler -> indices
    # The indices are passed to dataset_fetcher to get data from dataset.
    # data_fetcher -> collate_fn(dataset[index]) -> data_sample
    # we use batch_sampler to get correct data idx
    loader_indices = data_loader.batch_sampler

    # lsaturated_dict = {k: 0 for k in dataset.CLASSES}
    # rsaturated_dict = {k: 0 for k in dataset.CLASSES}
    # saturated_dict = {k: 0 for k in dataset.CLASSES}

    for batch_indices, data in zip(loader_indices, data_loader):
        with torch.no_grad():
            result, seg_logit = model(return_loss=False, **data)  # returns labels and logits

        seg_logit = seg_logit.detach()
        seg_gt = dataset.get_gt_seg_map_by_idx_and_reduce_zero_label(batch_indices[0])

        # lsaturated = seg_logit.lt(-10.)
        # rsaturated = seg_logit.gt(10.)
        # saturated = torch.logical_or(lsaturated, rsaturated)
        # if saturated.any():
        #     classes, count = np.unique(seg_gt[saturated.any(1).squeeze().cpu().numpy()], return_counts=True)
        #     for c, cc in zip(classes, count):
        #         if c != 255:
        #             saturated_dict[dataset.CLASSES[c]] += cc
        # if lsaturated.any():
        #     lclasses, lcount = np.unique(seg_gt[lsaturated.any(1).squeeze().cpu().numpy()], return_counts=True)
        #     for c, cc in zip(lclasses, lcount):
        #         if c != 255:
        #             lsaturated_dict[dataset.CLASSES[c]] += cc
        # if rsaturated.any():
        #     rclasses, rcount = np.unique(seg_gt[rsaturated.any(1).squeeze().cpu().numpy()], return_counts=True)
        #     for c, cc in zip(rclasses, rcount):
        #         if c != 255:
        #             rsaturated_dict[dataset.CLASSES[c]] += cc

        if (show or out_dir):
            # produce 3 images
            # gt_seg_map, pred_seg_map, confidence_map
            img_tensor = data['img'][0]
            img_metas = data['img_metas'][0].data[0]
            imgs = tensor2imgs(img_tensor, **img_metas[0]['img_norm_cfg'])

            assert len(imgs) == len(img_metas)

            for img, img_meta in zip(imgs, img_metas):
                h, w, _ = img_meta['img_shape']
                img_show = img[:h, :w, :]

                ori_h, ori_w = img_meta['ori_shape'][:-1]
                img_show = mmcv.imresize(img_show, (ori_w, ori_h))
                if out_dir:
                    out_file = osp.join(out_dir, img_meta['ori_filename'])
                else:
                    out_file = None
                # Todo implement bg mask and set it to transparent color
                ign_mask = (seg_gt == dataset.ignore_index).astype(np.uint8)
                _result = result[0]
                _result = np.ma.array(_result, mask=ign_mask)
                model.module.show_result(
                    img_show,
                    [_result],
                    palette=dataset.PALETTE,
                    show=show,
                    out_file=out_file,
                    opacity=opacity)

                model.module.show_result(
                    img_show,
                    [seg_gt, ],
                    palette=dataset.PALETTE,
                    show=show,
                    out_file=out_file[:-4] + "_gt" + out_file[-4:],
                    opacity=opacity)

                # max prob confidence map
                if not getattr(model.module.decode_head, 'use_bags', False):
                    if model.module.decode_head.loss_decode.loss_name.startswith("loss_edl"):
                        num_cls = seg_logit.shape[1]
                        alpha = model.module.decode_head.loss_decode.logit2evidence(seg_logit) + 1
                        if model.module.decode_head.loss_decode.pow_alpha:
                            alpha = alpha**2
                        probs = alpha / alpha.sum(dim=1, keepdim=True)
                        u = num_cls / alpha.sum(dim=1, keepdim=True)
                        dissonance = diss(alpha)

                        plot_conf(np.ma.array(u.cpu().numpy(), mask=ign_mask),
                                  out_file[: -4] + "_edl_u" + out_file[-4:])
                        plot_conf(np.ma.array(dissonance.cpu().numpy(), mask=ign_mask),
                                  out_file[: -4] + "_edl_diss" + out_file[-4:])
                        plot_conf(np.ma.array(probs.max(dim=1)[0].cpu().numpy(), mask=ign_mask),

                                  out_file[: -4] + "_edl_conf" + out_file[-4:])
                    else:
                        probs = F.softmax(seg_logit, dim=1)
                        plot_conf(np.ma.array(probs.max(dim=1)[0].cpu().numpy(), mask=ign_mask),
                                  out_file[: -4] + "_sm_conf" + out_file[-4:])

                # Mask for edges between separate labels
                plot_mask(dataset.edge_detector(seg_gt).cpu().numpy(), out_file[: -4] + "_edge_mask" + out_file[-4:])
                # Mask of ood samples
                if hasattr(dataset, "ood_indices"):
                    plot_mask((seg_gt == dataset.ood_indices[0]).astype(np.uint8), out_file[:-4] + "_ood_mask" + out_file[-4:])

        if efficient_test:
            result = [np2tmp(_, tmpdir='.efficient_test') for _ in result]

        if format_only:
            result = dataset.format_results(result, indices=batch_indices, **format_args)

        if pre_eval:
            # TODO: adapt samples_per_gpu > 1.
            # only samples_per_gpu=1 valid now
            # For originally included metrics mIOU
            result_seg = dataset.pre_eval(result, indices=batch_indices)[0]
            # For added metrics OOD, calibration
            if is_module_wrapper(model):
                _model = model.module
            else:
                _model = model

            # If using mmrazor
            if isinstance(_model, BaseAlgorithm):
                _model = _model.architecture.model

            if not getattr(_model.decode_head, 'use_bags', False):
                if _model.decode_head.loss_decode.loss_name.startswith("loss_edl"):
                    # For EDL probs
                    def logit2alpha(x):
                        ev = _model.decode_head.loss_decode.logit2evidence(x) + 1
                        if _model.decode_head.loss_decode.pow_alpha:
                            ev = ev**2
                        return ev
                    result_oth = dataset.pre_eval_custom(seg_logit, seg_gt, "edl", logit_fn=logit2alpha)
                else:
                    if _model.decode_head.loss_decode.use_softplus:
                        def logit2prob(x):
                            return F.softplus(x) / F.softplus(x).sum(dim=1, keepdim=True)
                    else:
                        def logit2prob(x):
                            return F.softmax(x, dim=1)
                    # For softmax probs
                    result_oth = dataset.pre_eval_custom(seg_logit, seg_gt, "softmax", logit_fn=logit2prob)
            else:
                result_oth = dataset.pre_eval_custom(seg_logit, seg_gt, "softmax",
                                                     model.module.decode_head.use_bags,
                                                     model.module.decode_head.bags_kwargs)
            result = [(result_seg, result_oth)]
            results.extend(result)
        else:
            results.extend(result)

        batch_size = len(result)
        for _ in range(batch_size):
            prog_bar.update()

    return results


def multi_gpu_test(model,
                   data_loader,
                   tmpdir=None,
                   gpu_collect=False,
                   efficient_test=False,
                   pre_eval=False,
                   format_only=False,
                   format_args={}):
    """Test model with multiple gpus by progressive mode.

    This method tests model with multiple gpus and collects the results
    under two different modes: gpu and cpu modes. By setting 'gpu_collect=True'
    it encodes results to gpu tensors and use gpu communication for results
    collection. On cpu mode it saves the results on different gpus to 'tmpdir'
    and collects them by the rank 0 worker.

    Args:
        model (nn.Module): Model to be tested.
        data_loader (utils.data.Dataloader): Pytorch data loader.
        tmpdir (str): Path of directory to save the temporary results from
            different gpus under cpu mode. The same path is used for efficient
            test. Default: None.
        gpu_collect (bool): Option to use either gpu or cpu to collect results.
            Default: False.
        efficient_test (bool): Whether save the results as local numpy files to
            save CPU memory during evaluation. Mutually exclusive with
            pre_eval and format_results. Default: False.
        pre_eval (bool): Use dataset.pre_eval() function to generate
            pre_results for metric evaluation. Mutually exclusive with
            efficient_test and format_results. Default: False.
        format_only (bool): Only format result for results commit.
            Mutually exclusive with pre_eval and efficient_test.
            Default: False.
        format_args (dict): The args for format_results. Default: {}.

    Returns:
        list: list of evaluation pre-results or list of save file names.
    """
    if efficient_test:
        warnings.warn(
            'DeprecationWarning: ``efficient_test`` will be deprecated, the '
            'evaluation is CPU memory friendly with pre_eval=True')
        mmcv.mkdir_or_exist('.efficient_test')
    # when none of them is set true, return segmentation results as
    # a list of np.array.
    assert [efficient_test, pre_eval, format_only].count(True) <= 1, \
        '``efficient_test``, ``pre_eval`` and ``format_only`` are mutually ' \
        'exclusive, only one of them could be true .'

    model.eval()
    results = []
    dataset = data_loader.dataset
    # The pipeline about how the data_loader retrieval samples from dataset:
    # sampler -> batch_sampler -> indices
    # The indices are passed to dataset_fetcher to get data from dataset.
    # data_fetcher -> collate_fn(dataset[index]) -> data_sample
    # we use batch_sampler to get correct data idx

    # batch_sampler based on DistributedSampler, the indices only point to data
    # samples of related machine.
    loader_indices = data_loader.batch_sampler

    rank, world_size = get_dist_info()
    if rank == 0:
        prog_bar = mmcv.ProgressBar(len(dataset))

    for batch_indices, data in zip(loader_indices, data_loader):
        with torch.no_grad():
            result, seg_logit = model(return_loss=False, rescale=True, **data)

        seg_logit = seg_logit.detach()
        seg_gt = dataset.get_gt_seg_map_by_idx_and_reduce_zero_label(batch_indices[0])

        if efficient_test:
            result = [np2tmp(_, tmpdir='.efficient_test') for _ in result]

        if format_only:
            result = dataset.format_results(result, indices=batch_indices, **format_args)
        if pre_eval:
            # TODO: adapt samples_per_gpu > 1.
            # only samples_per_gpu=1 valid now
            # result = dataset.pre_eval(result, indices=batch_indices)
            result_seg = dataset.pre_eval(result, indices=batch_indices)[0]
            if is_module_wrapper(model):
                _model = model.module
            else:
                _model = model

            # If using mmrazor
            if isinstance(_model, BaseAlgorithm):
                _model = _model.architecture.model

            # For added metrics OOD, calibration
            if not getattr(_model.decode_head, 'use_bags', False):
                if _model.decode_head.loss_decode.loss_name.startswith("loss_edl"):
                    # For EDL probs
                    def logit2alpha(x):
                        ev = _model.decode_head.loss_decode.logit2evidence(x) + 1
                        if _model.decode_head.loss_decode.pow_alpha:
                            ev = ev**2
                        return ev
                    result_oth = dataset.pre_eval_custom(seg_logit, seg_gt, "edl", logit_fn=logit2alpha)
                else:
                    if _model.decode_head.loss_decode.use_softplus:
                        def logit2prob(x):
                            return F.softplus(x) / F.softplus(x).sum(dim=1, keepdim=True)
                    else:
                        def logit2prob(x):
                            return F.softmax(x, dim=1)
                    # For softmax probs
                    result_oth = dataset.pre_eval_custom(seg_logit, seg_gt, "softmax", logit_fn=logit2prob)
            else:
                result_oth = dataset.pre_eval_custom(seg_logit, seg_gt, "softmax",
                                                     _model.decode_head.use_bags,
                                                     _model.decode_head.bags_kwargs)
            result = [(result_seg, result_oth)]
            results.extend(result)
        else:
            results.extend(result)

        if rank == 0:
            batch_size = len(result) * world_size
            for _ in range(batch_size):
                prog_bar.update()

    # collect results from all ranks
    if gpu_collect:
        results = collect_results_gpu(results, len(dataset))
    else:
        results = collect_results_cpu(results, len(dataset), tmpdir)
    return results
