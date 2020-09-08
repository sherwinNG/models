#  Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import time
import argparse
import ast
import wget
import tarfile
import logging
import numpy as np
import paddle.fluid as fluid
import glob
from paddle.fluid.dygraph.base import to_variable
from model import TSN_ResNet
from utils.config_utils import *
from reader.ucf101_reader import UCF101Reader

logging.root.handlers = []
FORMAT = '[%(levelname)s: %(filename)s: %(lineno)4d]: %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT, stream=sys.stdout)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser("Paddle Video train script")
    parser.add_argument(
        '--config',
        type=str,
        default='tsn.yaml',
        help='path to config file of model')
    parser.add_argument(
        '--batch_size',
        type=int,
        default=None,
        help='training batch size. None to use config file setting.')
    parser.add_argument(
        '--pretrain',
        type=str,
        default=None,
        help='path to pretrain weights. None to use default weights path in  ~/.paddle/weights.'
    )
    parser.add_argument(
        '--resume',
        type=str,
        default=None,
        help='path to resume training based on previous checkpoints. '
        'None for not resuming any checkpoints.')
    parser.add_argument(
        '--use_gpu',
        type=ast.literal_eval,
        default=True,
        help='default use gpu.')
    parser.add_argument(
        '--epoch',
        type=int,
        default=None,
        help='epoch number, 0 for read from config file')
    parser.add_argument(
        '--use_data_parallel',
        type=ast.literal_eval,
        default=True,
        help='default use data parallel.')
    parser.add_argument(
        '--checkpoint',
        type=str,
        default="./checkpoint",
        help='path to resume training based on previous checkpoints. '
        'None for not resuming any checkpoints.')
    parser.add_argument(
        '--weights',
        type=str,
        default="./weights",
        help='path to save the final optimized model.'
        'default path is "./weights".')
    parser.add_argument(
        '--validate',
        type=str,
        default=True,
        help='whether to validating in training phase.'
        'default value is True.')
    args = parser.parse_args()
    return args


def decompress(path):
    t = tarfile.open(path)
    print("path[0] {}".format(os.path.split(path)[0]))
    t.extractall(path=os.path.split(path)[0])
    t.close()


def download(url, path):
    weight_dir = os.path.split(path)[0]
    if not os.path.exists(weight_dir):
        os.makedirs(weight_dir)

    path = path + ".tar.gz"
    print("path {}".format(path))
    wget.download(url, path)
    decompress(path)


def pretrain_info():
    return (
        'ResNet50_pretrained',
        'https://paddlemodels.bj.bcebos.com/video_classification/ResNet50_pretrained.tar.gz'
    )


def download_pretrained(pretrained):
    if pretrained is not None:
        WEIGHT_DIR = pretrained
    else:
        WEIGHT_DIR = os.path.join(os.path.expanduser('~'), '.paddle', 'weights')

    path, url = pretrain_info()
    if not path:
        return None

    path = os.path.join(WEIGHT_DIR, path)
    if not os.path.isdir(WEIGHT_DIR):
        logger.info('{} not exists, will be created automatically.'.format(
            WEIGHT_DIR))
        os.makedirs(WEIGHT_DIR)
    if os.path.exists(path):
        return path
    logger.info("Download pretrain weights of ResNet50 from {}".format(url))
    download(url, path)
    return path


def init_model(model, pre_state_dict):
    param_state_dict = {}
    model_dict = model.state_dict()
    for key in model_dict.keys():
        weight_name = model_dict[key].name
        if weight_name in pre_state_dict.keys(
        ) and weight_name != "fc_0.w_0" and weight_name != "fc_0.b_0":
            print('Load weight: {}, shape: {}'.format(
                weight_name, pre_state_dict[weight_name].shape))
            param_state_dict[key] = pre_state_dict[weight_name]
        else:
            param_state_dict[key] = model_dict[key]
    model.set_dict(param_state_dict)
    return model


def val(epoch, model, cfg, args):
    reader = UCF101Reader(name="TSN", mode="valid", cfg=cfg)
    reader = reader.create_reader()
    total_loss = 0.0
    total_acc1 = 0.0
    total_acc5 = 0.0
    total_sample = 0

    for batch_id, data in enumerate(reader()):
        x_data = np.array([item[0] for item in data])
        y_data = np.array([item[1] for item in data]).reshape([-1, 1])
        imgs = to_variable(x_data)
        labels = to_variable(y_data)
        labels.stop_gradient = True

        outputs = model(imgs)

        loss = fluid.layers.cross_entropy(
            input=outputs, label=labels, ignore_index=-1)
        avg_loss = fluid.layers.mean(loss)
        acc_top1 = fluid.layers.accuracy(input=outputs, label=labels, k=1)
        acc_top5 = fluid.layers.accuracy(input=outputs, label=labels, k=5)

        dy_out = avg_loss.numpy()[0]
        total_loss += dy_out
        total_acc1 += acc_top1.numpy()[0]
        total_acc5 += acc_top5.numpy()[0]
        total_sample += 1

        if batch_id % 5 == 0:
            print(
                "TEST Epoch {}, iter {}, loss={:.5f}, acc1 {:.5f}, acc5 {:.5f}".
                format(epoch, batch_id, total_loss / total_sample, total_acc1 /
                       total_sample, total_acc5 / total_sample))

    print('Finish loss {} , acc1 {} , acc5 {}'.format(
        total_loss / total_sample, total_acc1 / total_sample, total_acc5 /
        total_sample))
    return total_acc1 / total_sample


def create_optimizer(cfg, params):
    total_videos = cfg.total_videos
    step = int(total_videos / cfg.batch_size + 1)
    bd = [e * step for e in cfg.decay_epochs]
    base_lr = cfg.learning_rate
    lr_decay = cfg.learning_rate_decay
    lr = [base_lr, base_lr * lr_decay, base_lr * lr_decay * lr_decay]
    l2_weight_decay = cfg.l2_weight_decay
    momentum = cfg.momentum

    optimizer = fluid.optimizer.Momentum(
        learning_rate=fluid.layers.piecewise_decay(
            boundaries=bd, values=lr),
        momentum=momentum,
        regularization=fluid.regularizer.L2Decay(l2_weight_decay),
        parameter_list=params)

    return optimizer


def train(args):
    config = parse_config(args.config)
    train_config = merge_configs(config, 'train', vars(args))
    valid_config = merge_configs(config, 'valid', vars(args))
    print_configs(train_config, 'Train')

    # get the pretrained weights
    pretrained_path = download_pretrained(args.pretrain)

    use_data_parallel = args.use_data_parallel
    trainer_count = fluid.dygraph.parallel.Env().nranks

    # (data_parallel step1/6)
    place = fluid.CUDAPlace(fluid.dygraph.parallel.Env().dev_id) \
        if use_data_parallel else fluid.CUDAPlace(0)
    pre_state_dict = fluid.load_program_state(pretrained_path)

    with fluid.dygraph.guard(place):
        if use_data_parallel:
            # (data_parallel step2/6)
            strategy = fluid.dygraph.parallel.prepare_context()
        video_model = TSN_ResNet(train_config)
        video_model = init_model(video_model, pre_state_dict)
        optimizer = create_optimizer(train_config.TRAIN,
                                     video_model.parameters())

        if use_data_parallel:
            # (data_parallel step3/6)
            video_model = fluid.dygraph.parallel.DataParallel(video_model,
                                                              strategy)

        bs_denominator = 1
        if args.use_gpu:
            # check number of GPUs
            gpus = os.getenv("CUDA_VISIBLE_DEVICES", "")
            if gpus == "":
                pass
            else:
                gpus = gpus.split(",")
                num_gpus = len(gpus)
            bs_denominator = num_gpus
        train_config.TRAIN.batch_size = int(train_config.TRAIN.batch_size /
                                            bs_denominator)

        train_reader = UCF101Reader(name="TSN", mode="train", cfg=train_config)
        train_reader = train_reader.create_reader()

        if use_data_parallel:
            # (data_parallel step4/6)
            train_reader = fluid.contrib.reader.distributed_batch_reader(
                train_reader)

        # resume training the model
        if args.resume is not None:
            model_state, opt_state = fluid.load_dygraph(args.resume)
            video_model.set_dict(model_state)
            optimizer.set_dict(opt_state)

        for epoch in range(1, train_config.TRAIN.epoch + 1):
            video_model.train()
            total_loss = 0.0
            total_acc1 = 0.0
            total_acc5 = 0.0
            total_sample = 0
            batch_start = time.time()
            for batch_id, data in enumerate(train_reader()):
                train_reader_cost = time.time() - batch_start
                x_data = np.array([item[0] for item in data]).astype("float32")
                y_data = np.array([item[1] for item in data]).reshape([-1, 1])
                imgs = to_variable(x_data)
                labels = to_variable(y_data)
                labels.stop_gradient = True
                outputs = video_model(imgs)

                loss = fluid.layers.cross_entropy(
                    input=outputs, label=labels, ignore_index=-1)
                avg_loss = fluid.layers.mean(loss)

                acc_top1 = fluid.layers.accuracy(
                    input=outputs, label=labels, k=1)
                acc_top5 = fluid.layers.accuracy(
                    input=outputs, label=labels, k=5)
                dy_out = avg_loss.numpy()[0]

                if use_data_parallel:
                    # (data_parallel step5/6)
                    avg_loss = video_model.scale_loss(avg_loss)
                    avg_loss.backward()
                    video_model.apply_collective_grads()
                else:
                    avg_loss.backward()

                optimizer.minimize(avg_loss)
                video_model.clear_gradients()

                total_loss += dy_out
                total_acc1 += acc_top1.numpy()[0]
                total_acc5 += acc_top5.numpy()[0]
                total_sample += 1
                train_batch_cost = time.time() - batch_start
                print(
                    'TRAIN Epoch: {}, iter: {}, batch_cost: {:.5f} s, reader_cost: {:.5f} s, loss={:.6f}, acc1 {:.6f}, acc5 {:.6f}  '.
                    format(epoch, batch_id, train_batch_cost, train_reader_cost,
                           total_loss / total_sample, total_acc1 / total_sample,
                           total_acc5 / total_sample))
                batch_start = time.time()

            print(
                'TRAIN End, Epoch {}, avg_loss= {}, avg_acc1= {}, avg_acc5= {}'.
                format(epoch, total_loss / total_sample, total_acc1 /
                       total_sample, total_acc5 / total_sample))

            # save model's and optimizer's parameters which used for resuming the training stage
            save_parameters = (not use_data_parallel) or (
                use_data_parallel and
                fluid.dygraph.parallel.Env().local_rank == 0)
            if save_parameters:
                model_path_pre = "_tsn"
                if not os.path.isdir(args.checkpoint):
                    os.makedirs(args.checkpoint)
                model_path = os.path.join(
                    args.checkpoint,
                    "_" + model_path_pre + "_epoch{}".format(epoch))
                fluid.dygraph.save_dygraph(video_model.state_dict(), model_path)
                fluid.dygraph.save_dygraph(optimizer.state_dict(), model_path)

            if args.validate:
                video_model.eval()
                val_acc = val(epoch, video_model, valid_config, args)
                # save the best parameters in trainging stage
                if epoch == 1:
                    best_acc = val_acc
                else:
                    if val_acc > best_acc:
                        best_acc = val_acc
                        if fluid.dygraph.parallel.Env().local_rank == 0:
                            if not os.path.isdir(args.weights):
                                os.makedirs(args.weights)
                            fluid.dygraph.save_dygraph(video_model.state_dict(),
                                                       args.weights + "/final")
            else:
                if fluid.dygraph.parallel.Env().local_rank == 0:
                    if not os.path.isdir(args.weights):
                        os.makedirs(args.weights)
                    fluid.dygraph.save_dygraph(video_model.state_dict(),
                                               args.weights + "/final")

        logger.info('[TRAIN] training finished')


if __name__ == "__main__":
    args = parse_args()
    logger.info(args)
    train(args)