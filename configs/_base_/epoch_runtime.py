# yapf:disable
log_config = dict(
    interval=1,
    by_epoch=True,
    hooks=[dict(type='TextLoggerHook'),
           dict(type='TensorboardLoggerHook')
           ]
)
# yapf:enable
dist_params = dict(backend='nccl')
log_level = 'INFO'
load_from = None
resume_from = None
workflow = [('train', 1)]
cudnn_benchmark = True
