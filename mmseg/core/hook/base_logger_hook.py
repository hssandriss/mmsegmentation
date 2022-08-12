# Copyright (c) OpenMMLab. All rights reserved.
import numbers
from abc import ABCMeta, abstractmethod
from typing import Dict

import numpy as np
import torch

from mmcv.runner.hooks import Hook


class LoggerHook_(Hook):
    """Base class for logger hooks.
    Args:
        interval (int): Logging interval (every k iterations). Default 10.
        ignore_last (bool): Ignore the log of last iterations in each epoch
            if less than `interval`. Default True.
        reset_flag (bool): Whether to clear the output buffer after logging.
            Default False.
        by_epoch (bool): Whether EpochBasedRunner is used. Default True.
    """

    __metaclass__ = ABCMeta

    def __init__(self,
                 interval: int = 10,
                 ignore_last: bool = True,
                 reset_flag: bool = False,
                 by_epoch: bool = True):
        self.interval = interval
        self.ignore_last = ignore_last
        self.reset_flag = reset_flag
        self.by_epoch = by_epoch

    @abstractmethod
    def log(self, runner):
        pass

    @staticmethod
    def is_scalar(val,
                  include_np: bool = True,
                  include_torch: bool = True) -> bool:
        """Tell the input variable is a scalar or not.
        Args:
            val: Input variable.
            include_np (bool): Whether include 0-d np.ndarray as a scalar.
            include_torch (bool): Whether include 0-d torch.Tensor as a scalar.
        Returns:
            bool: True or False.
        """
        if isinstance(val, numbers.Number):
            return True
        elif include_np and isinstance(val, np.ndarray) and val.ndim == 0:
            return True
        elif include_torch and isinstance(val, torch.Tensor) and len(val) == 1:
            return True
        else:
            return False

    def get_mode(self, runner) -> str:
        if runner.mode == 'train':
            if 'time' in runner.log_buffer.output:
                mode = 'train'
            else:
                mode = 'val'
        elif runner.mode == 'val':
            mode = 'val'
        else:
            raise ValueError(f"runner mode should be 'train' or 'val', "
                             f'but got {runner.mode}')
        return mode

    def get_epoch(self, runner) -> int:
        if runner.mode == 'train':
            epoch = runner.epoch + 1
        elif runner.mode == 'val':
            # normal val mode
            # runner.epoch += 1 has been done before val workflow
            epoch = runner.epoch
        else:
            raise ValueError(f"runner mode should be 'train' or 'val', "
                             f'but got {runner.mode}')
        return epoch

    def get_iter(self, runner, inner_iter: bool = False) -> int:
        """Get the current training iteration step."""
        if self.by_epoch and inner_iter:
            current_iter = runner.inner_iter + 1
        else:
            current_iter = runner.iter + 1
        return current_iter

    def get_lr_tags(self, runner) -> Dict[str, float]:
        tags = {}
        lrs = runner.current_lr()
        if isinstance(lrs, dict):
            for name, value in lrs.items():
                tags[f'learning_rate/{name}'] = value[0]
        else:
            tags['learning_rate'] = lrs[0]
        return tags

    def get_momentum_tags(self, runner) -> Dict[str, float]:
        tags = {}
        momentums = runner.current_momentum()
        if isinstance(momentums, dict):
            for name, value in momentums.items():
                tags[f'momentum/{name}'] = value[0]
        else:
            tags['momentum'] = momentums[0]
        return tags

    def get_loggable_tags(
        self,
        runner,
        allow_scalar: bool = True,
        allow_text: bool = False,
        add_mode: bool = True,
        tags_to_skip: tuple = ('time', 'data_time')
    ) -> Dict:
        tags = {}
        for var, val in runner.log_buffer.output.items():
            if var in tags_to_skip:
                continue
            if self.is_scalar(val) and not allow_scalar:
                continue
            if isinstance(val, str) and not allow_text:
                continue
            if add_mode:
                var = f'{self.get_mode(runner)}/{var}'
            tags[var] = val
        tags.update(self.get_lr_tags(runner))
        tags.update(self.get_momentum_tags(runner))
        return tags

    def before_run(self, runner) -> None:
        for hook in runner.hooks[::-1]:
            if isinstance(hook, LoggerHook_):
                hook.reset_flag = True
                break

    def before_epoch(self, runner) -> None:
        pass

    def after_train_iter(self, runner) -> None:
        pass

    def after_train_epoch(self, runner) -> None:
        if self.by_epoch and self.every_n_epochs(runner, self.interval):
            import ipdb; ipdb.set_trace()
            runner.log_buffer.average()
        if runner.log_buffer.ready:
            self.log(runner)
            if self.reset_flag:
                runner.log_buffer.clear_output()

    def after_val_epoch(self, runner) -> None:
        runner.log_buffer.average()
        self.log(runner)
        if self.reset_flag:
            runner.log_buffer.clear_output()
