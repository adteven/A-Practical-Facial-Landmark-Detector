#!/usr/bin/env python3
#-*- coding:utf-8 -*-

import argparse
import logging
import os

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torchvision import transforms
from tensorboardX import SummaryWriter

from dataloader.WLFW import WLFWDatasets
from model.pfld import PFLDInference, AuxiliaryNet
from loss.loss import PFLDLoss
from utils.utils import AverageMeter


def print_args(args):
    for arg in vars(args):
        s = arg + ': ' + str(getattr(args, arg))
        logging.info(s)


def save_checkpoint(state, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    logging.info('Save checkpoint to {0:}'.format(filename))


def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected')


def train(train_loader, plfd_backbone, auxiliarynet, GPUs, criterion, optimizer,
          epoch):
    losses = AverageMeter()

    # set CUDA_VISIBLE_DEVICES
    if len(GPUs) > 0:
        gpus = ','.join([str(x) for x in GPUs])
        os.environ['CUDA_VISIBLE_DEVICES'] = gpus
        GPUs = tuple(range(len(GPUs)))
        # logging.info('Set CUDA_VISIBLE_DEVICES to {}...'.format(GPUs))
        plfd_backbone = nn.DataParallel(plfd_backbone, device_ids=GPUs)
        plfd_backbone = plfd_backbone.cuda()
        auxiliarynet = nn.DataParallel(auxiliarynet, device_ids=GPUs)
        auxiliarynet = auxiliarynet.cuda()

    for img, landmark_gt, attribute_gt, euler_angle_gt in train_loader:
        img.requires_grad = False
        img = img.cuda(non_blocking=True)

        attribute_gt.requires_grad = False
        attribute_gt = attribute_gt.cuda(non_blocking=True)

        landmark_gt.requires_grad = False
        landmark_gt = landmark_gt.cuda(non_blocking=True)

        euler_angle_gt.requires_grad = False
        euler_angle_gt = euler_angle_gt.cuda(non_blocking=True)

        features, landmarks = plfd_backbone(img)
        angle = auxiliarynet(features)
        weighted_loss, loss = criterion(attribute_gt, landmark_gt, euler_angle_gt,
                                    angle, landmarks, args.train_batchsize)

        optimizer.zero_grad()
        weighted_loss.backward()
        optimizer.step()

        losses.update(loss.item())
    return weighted_loss, loss


def validate(wlfw_val_dataloader, plfd_backbone, auxiliarynet, criterion,
             epoch):
    plfd_backbone.eval()
    auxiliarynet.eval()

    losses = []
    with torch.no_grad():
        for img, landmark_gt, attribute_gt, euler_angle_gt in wlfw_val_dataloader:
            img.requires_grad = False
            img = img.cuda(non_blocking=True)

            attribute_gt.requires_grad = False
            attribute_gt = attribute_gt.cuda(non_blocking=True)

            landmark_gt.requires_grad = False
            landmark_gt = landmark_gt.cuda(non_blocking=True)

            euler_angle_gt.requires_grad = False
            euler_angle_gt = euler_angle_gt.cuda(non_blocking=True)

            plfd_backbone = plfd_backbone.cuda()
            auxiliarynet = auxiliarynet.cuda()

            _, landmark = plfd_backbone(img)

            loss = torch.mean(
                torch.sum((landmark_gt - landmark)**2,axis=1))
            losses.append(loss.cpu().numpy())

    return np.mean(losses)


def main(args):
    # Step 1: parse args config
    logging.basicConfig(
        format=
        '[%(asctime)s] [p%(process)s] [%(pathname)s:%(lineno)d] [%(levelname)s] %(message)s',
        level=logging.INFO,
        handlers=[
            logging.FileHandler(args.log_file, mode='w'),
            logging.StreamHandler()
        ])
    print_args(args)

    GPUs = [int(x) for x in args.devices_id.split(',')]

    # Step 2: model, criterion, optimizer, scheduler
    plfd_backbone = PFLDInference()
    auxiliarynet = AuxiliaryNet()
    if args.resume != '':
        logging.info('Load the checkpoint:{}'.format(args.resume))
        checkpoint = torch.load(args.resume)
        plfd_backbone.load_state_dict(checkpoint['plfd_backbone'])
        auxiliarynet.load_state_dict(checkpoint['auxiliarynet'])
    criterion = PFLDLoss()
    optimizer = torch.optim.Adam(
        [{
            'params': plfd_backbone.parameters()
        }, {
            'params': auxiliarynet.parameters()
        }],
        lr=args.base_lr,
        weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode='min', patience=args.lr_patience, verbose=True)

    # step 3: data
    # argumetion
    transform = transforms.Compose([transforms.ToTensor()])
    wlfwdataset = WLFWDatasets(args.dataroot, transform)

    train_batchsize = args.train_batchsize if len(GPUs) == 0 else args.train_batchsize * len(GPUs)
    val_batchsize = args.val_batchsize if len(GPUs) == 0 else args.val_batchsize * len(GPUs)

    dataloader = DataLoader(
        wlfwdataset,
        batch_size=train_batchsize,
        shuffle=True,
        num_workers=args.workers,
        drop_last=False)

    wlfw_val_dataset = WLFWDatasets(args.val_dataroot, transform)
    wlfw_val_dataloader = DataLoader(
        wlfw_val_dataset,
        batch_size=val_batchsize,
        shuffle=False,
        num_workers=args.workers)

    # step 4: run
    writer = SummaryWriter(args.tensorboard)
    for epoch in range(args.start_epoch, args.end_epoch + 1):
        weighted_train_loss, train_loss = train(dataloader, plfd_backbone, auxiliarynet, GPUs,
                                      criterion, optimizer, epoch)
        filename = os.path.join(
            str(args.snapshot), "checkpoint_epoch_" + str(epoch) + '.pth.tar')
        save_checkpoint({
            'epoch': epoch,
            'plfd_backbone': plfd_backbone.state_dict(),
            'auxiliarynet': auxiliarynet.state_dict()
        }, filename)

        val_loss = validate(wlfw_val_dataloader, plfd_backbone, auxiliarynet,
                            criterion, epoch)

        scheduler.step(val_loss)
        writer.add_scalar('data/weighted_loss', weighted_train_loss, epoch)
        writer.add_scalars('data/loss', {'val loss': val_loss, 'train loss': train_loss}, epoch)
    writer.close()


def parse_args():
    parser = argparse.ArgumentParser(description='A Practical Facial Landmark Detector Training')
    # general
    parser.add_argument('-j', '--workers', default=16, type=int)
    parser.add_argument('--devices_id', default='0', type=str)  #TBD
    parser.add_argument('--test_initial', default='false', type=str2bool)  #TBD

    # training
    ##  -- optimizer
    parser.add_argument('--base_lr', default=0.0001, type=int)
    parser.add_argument('--weight-decay', '--wd', default=1e-6, type=float)

    # -- lr
    parser.add_argument("--lr_patience", default=40, type=int)

    # -- epoch
    parser.add_argument('--start_epoch', default=1, type=int)
    parser.add_argument('--end_epoch', default=1000, type=int)

    # -- snapshot、tensorboard log and checkpoint
    parser.add_argument(
        '--snapshot',
        default='./checkpoint/snapshot/',
        type=str,
        metavar='PATH')
    parser.add_argument(
        '--log_file', default="./checkpoint/train.logs", type=str)
    parser.add_argument(
        '--tensorboard', default="./checkpoint/tensorboard", type=str)
    parser.add_argument(
        '--resume', default='', type=str, metavar='PATH')  # TBD

    # --dataset
    parser.add_argument(
        '--dataroot',
        default='./data/train_data/list.txt',
        type=str,
        metavar='PATH')
    parser.add_argument(
        '--val_dataroot',
        default='./data/test_data/list.txt',
        type=str,
        metavar='PATH')
    parser.add_argument('--train_batchsize', default=128, type=int)
    parser.add_argument('--val_batchsize', default=8, type=int)
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()
    main(args)
