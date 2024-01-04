import warnings, os, random, torch

warnings.filterwarnings("ignore")
os.environ['CUDA_VISIBLE_DEVICES'] = '0'  # 可用的GPU
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"

from config import config
from utils.writer import JsonFile
from utils.processor_utils import *


def get_model(args, model_name=None):
    if model_name is None:
        model_name = [args.model['name'], args.model['backbone']]

    if model_name[-1] is not None:  ## 框架模型
        if 'seel' in model_name: from models.img.SEEL_IMG import import_model
        backbone, dataset = get_model(args, [model_name[-1], None])
        model = import_model(args, backbone)
    else:  ## 非框架模型
        if 'resnet' in model_name:  from models.img.ResNet import import_model

        model, dataset = import_model(args)
        init_weight(model)

    model = model.to(args.train['device'])
    return model, dataset


def run(args):
    args = random_parameters(args)
    model, dataset = get_model(args)
    if torch.cuda.device_count() > 1:  # 指定卡/多卡 训练
        model = torch.nn.DataParallel(model, device_ids=args.train['device_ids'])

    if dataset.task == 'cls':
        from utils.processor import Processor
    else:
        from utils.processor_gen import Processor

    dataset.metrics = ['macro_f1', 'accuracy']
    dataset.lab_range = list(range(dataset.n_class))
    processor = Processor(args, model, dataset)
    result = processor._train()

    torch.save(model.metrics,
               f"./saves/ce_1_scl_{args.model['scl']}_seel_{args.model['seel']}_seed_{args.train['seed']}.pt")
    ## 2. 输出统计结果
    record = {
        'params': {
            'e': args.train['epochs'],
            'es': args.train['early_stop'],
            'lr': args.train['learning_rate'],
            'lr_pre': args.train['learning_rate_pre'],
            'bz': args.train['batch_size'],
            'dr': args.model['drop_rate'],
            'seed': args.train['seed'],
        },
        'metric': {
            'stop': result['epoch'],

            'tv_mf1': result['valid']['macro_f1'],
            'tv_acc': result['valid']['accuracy'],
            # 'te_mf1':  result['test']['macro_f1'],
        },
    }
    return record


if __name__ == '__main__':

    """
    tasks: 
        rest, lap, twi

    frameworks: 
        fw_rcl: retrieval contrrast learning
        fw_atp: all token prediction
        
    models: 
        tnet:
        memnet:
        bert:
        deberta:
        aclt: EMNLP 2021 (Bert_Based)
        cscl: our sota
    """
    args = config(tasks=['img', 'cifar100'], models=['seel', 'resnet'])
    # args = config(tasks=['absa','lap'], models=['memnet', None])

    ## Parameters Settings
    args.model['scale'] = 'base'

    args.train['epochs'] = 120
    args.train['early_stop'] = 30
    args.train['batch_size'] = 256
    args.train['save_model'] = 1
    args.train['log_step_rate'] = 2.0
    args.train['learning_rate'] = 1e-1
    args.train['learning_rate_pre'] = 1e-1

    args.train['split'] = 0.5

    args.model['drop_rate'] = 0.3
    args.train['do_test'] = 0
    args.train['inference'] = 0
    args.train['wandb'] = False
    args.train['show'] = 1

    seeds = [2024 + i for i in range(100)]
    ## Cycle Training
    os.makedirs(f"{args.file['record']}", exist_ok=True)
    recoed_path = f"{args.file['record']}{args.model['name']}_best.jsonl"
    record_show = JsonFile(recoed_path, mode_w='a', delete=True)
    for seed in seeds:
        args.train['seed'] = seed
        args.train['seed_change'] = False

        args.model['scl'], args.model['seel'] = 0, 0
        record = run(args)
        record_show.write(record, space=False)
