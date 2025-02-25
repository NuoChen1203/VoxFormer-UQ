# Copyright (c) 2022-2023, NVIDIA Corporation & Affiliates. All rights reserved.
#
# This work is made available under the Nvidia Source Code License-NC.
# To view a copy of this license, visit
# https://github.com/NVlabs/VoxFormer/blob/main/LICENSE


import os
import seaborn as sns
import matplotlib.pylab as plt

from tkinter.messagebox import NO
import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.runner import force_fp32, auto_fp16
from mmdet.models import DETECTORS
from mmdet3d.core import bbox3d2result
from mmdet3d.models.detectors.mvx_two_stage import MVXTwoStageDetector
import time
import copy
import numpy as np
import mmdet3d
from projects.mmdet3d_plugin.models.utils.bricks import run_time
from projects.mmdet3d_plugin.voxformer.utils.ssc_loss import sem_scal_loss, CE_ssc_loss, KL_sep, geo_scal_loss, BCE_ssc_loss

@DETECTORS.register_module()
class LMSCNet_SS(MVXTwoStageDetector):
    def __init__(self,
                 class_num=None,
                 input_dimensions=None,
                 out_scale=None,
                 gamma=0,
                 alpha=0.5,
                 pts_voxel_layer=None,
                 pts_voxel_encoder=None,
                 pts_middle_encoder=None,
                 pts_fusion_layer=None,
                 img_backbone=None,
                 pts_backbone=None,
                 img_neck=None,
                 pts_neck=None,
                 pts_bbox_head=None,
                 img_roi_head=None,
                 img_rpn_head=None,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None
                 ):

        super(LMSCNet_SS,
              self).__init__(pts_voxel_layer, pts_voxel_encoder,
                             pts_middle_encoder, pts_fusion_layer,
                             img_backbone, pts_backbone, img_neck, pts_neck,
                             pts_bbox_head, img_roi_head, img_rpn_head,
                             train_cfg, test_cfg, pretrained)
        '''
        SSCNet architecture
        :param N: number of classes to be predicted (i.e. 12 for NYUv2)
        '''

        super().__init__()
        self.out_scale=out_scale
        self.nbr_classes = class_num
        self.gamma = gamma
        self.alpha = alpha
        self.input_dimensions = input_dimensions  # Grid dimensions should be (W, H, D).. z or height being axis 1
        f = self.input_dimensions[1]

        self.pool = nn.MaxPool2d(2)  # [F=2; S=2; P=0; D=1]

        self.pooling = nn.MaxPool3d((2, 2, 2), stride=(2, 2, 2))
        self.class_frequencies_level1 =  np.array([5.41773033e09, 4.03113667e08])

        self.class_weights_level_1 = torch.from_numpy(
            1 / np.log(self.class_frequencies_level1 + 0.001)
        )

        dropout_p=0.3

        # self.drop1=nn.Dropout(dropout_p)
        # self.drop2=nn.Dropout(dropout_p)
        # self.drop3=nn.Dropout(dropout_p)

        self.drop1=nn.Identity()
        self.drop2=nn.Identity()
        self.drop3=nn.Identity()

        self.Encoder_block1 = nn.Sequential(
        nn.Conv2d(f, f, kernel_size=3, padding=1, stride=1),
        nn.ReLU(),
        nn.Conv2d(f, f, kernel_size=3, padding=1, stride=1),
        nn.ReLU(),
        # nn.Dropout(dropout_p)
        )

        self.Encoder_block2 = nn.Sequential(
        nn.MaxPool2d(2),
        nn.Conv2d(f, int(f*1.5), kernel_size=3, padding=1, stride=1),
        nn.ReLU(),
        nn.Conv2d(int(f*1.5), int(f*1.5), kernel_size=3, padding=1, stride=1),
        nn.ReLU(),
        # nn.Dropout(dropout_p)
        )

        self.Encoder_block3 = nn.Sequential(
        nn.MaxPool2d(2),
        nn.Conv2d(int(f*1.5), int(f*2), kernel_size=3, padding=1, stride=1),
        nn.ReLU(),
        nn.Conv2d(int(f*2), int(f*2), kernel_size=3, padding=1, stride=1),
        nn.ReLU(),
        # nn.Dropout(dropout_p)
        )

        self.Encoder_block4 = nn.Sequential(
        nn.MaxPool2d(2),
        nn.Conv2d(int(f*2), int(f*2.5), kernel_size=3, padding=1, stride=1),
        nn.ReLU(),
        nn.Conv2d(int(f*2.5), int(f*2.5), kernel_size=3, padding=1, stride=1),
        nn.ReLU(),
        # nn.Dropout(dropout_p)
        )

        # Treatment output 1:8
        self.conv_out_scale_1_8 = nn.Conv2d(int(f*2.5), int(f/8), kernel_size=3, padding=1, stride=1)
        self.deconv_1_8__1_2    = nn.ConvTranspose2d(int(f/8), int(f/8), kernel_size=4, padding=0, stride=4)
        self.deconv_1_8__1_1    = nn.ConvTranspose2d(int(f/8), int(f/8), kernel_size=8, padding=0, stride=8)

        # Treatment output 1:4
        if self.out_scale=="1_4" or self.out_scale=="1_2" or self.out_scale=="1_1":
          self.deconv1_8          = nn.ConvTranspose2d(int(f/8), int(f/8), kernel_size=6, padding=2, stride=2)
          self.conv1_4            = nn.Conv2d(int(f*2) + int(f/8), int(f*2), kernel_size=3, padding=1, stride=1)
          self.conv_out_scale_1_4 = nn.Conv2d(int(f*2), int(f/4), kernel_size=3, padding=1, stride=1)
          self.deconv_1_4__1_1    = nn.ConvTranspose2d(int(f/4), int(f/4), kernel_size=4, padding=0, stride=4)

        # Treatment output 1:2
        if self.out_scale=="1_2" or self.out_scale=="1_1":
          self.deconv1_4          = nn.ConvTranspose2d(int(f/4), int(f/4), kernel_size=6, padding=2, stride=2)
          self.conv1_2            = nn.Conv2d(int(f*1.5) + int(f/4) + int(f/8), int(f*1.5), kernel_size=3, padding=1, stride=1)
          self.conv_out_scale_1_2 = nn.Conv2d(int(f*1.5), int(f/2), kernel_size=3, padding=1, stride=1)

        # Treatment output 1:1
        if self.out_scale=="1_1":
          self.deconv1_2          = nn.ConvTranspose2d(int(f/2), int(f/2), kernel_size=6, padding=2, stride=2)
          self.conv1_1            = nn.Conv2d(int(f/8) + int(f/4) + int(f/2) + int(f), f, kernel_size=3, padding=1, stride=1)
        
        if self.out_scale=="1_1":
          self.seg_head_1_1 = SegmentationHead(1, 8, self.nbr_classes, [1, 2, 3])
        elif self.out_scale=="1_2":
          self.seg_head_1_2 = SegmentationHead(1, 8, self.nbr_classes, [1, 2, 3])
        elif self.out_scale=="1_4":
          self.seg_head_1_4 = SegmentationHead(1, 8, self.nbr_classes, [1, 2, 3])
        elif self.out_scale=="1_8":
          self.seg_head_1_8 = SegmentationHead(1, 8, self.nbr_classes, [1, 2, 3])

    def step(self, input):

        # input = x['3D_OCCUPANCY']  # Input to LMSCNet model is 3D occupancy big scale (1:1) [bs, 1, W, H, D]
        # input = torch.squeeze(input, dim=1).permute(0, 2, 1, 3)  # Reshaping to the right way for 2D convs [bs, H, W, D]

        # print(input.shape) [4, 32, 256, 256]

        # Encoder block
        _skip_1_1 = self.Encoder_block1(input)
        # print('_skip_1_1.shape', _skip_1_1.shape)  # [1, 32, 256, 256]
        _skip_1_2 = self.Encoder_block2(_skip_1_1)
        # print('_skip_1_2.shape', _skip_1_2.shape)  # [1, 48, 128, 128]
        _skip_1_4 = self.Encoder_block3(_skip_1_2) 
        # print('_skip_1_4.shape', _skip_1_4.shape)  # [1, 64, 64, 64]
        _skip_1_8 = self.Encoder_block4(_skip_1_4) 
        # print('_skip_1_8.shape', _skip_1_8.shape)  # [1, 80, 32, 32]

        # Out 1_8
        out_scale_1_8__2D = self.conv_out_scale_1_8(_skip_1_8)

        # print('out_scale_1_8__2D.shape', out_scale_1_8__2D.shape)  # [1, 4, 32, 32]

        # print(self.out_scale)

        if self.out_scale=="1_8":
          out_scale_1_8__3D = self.seg_head_1_8(out_scale_1_8__2D) # [1, 20, 16, 128, 128]
          out_scale_1_8__3D = out_scale_1_8__3D.permute(0, 1, 3, 4, 2) # [1, 20, 128, 128, 16]
          return out_scale_1_8__3D

        elif self.out_scale=="1_4":
          # Out 1_4
          out = self.deconv1_8(out_scale_1_8__2D)
          out = torch.cat((out, _skip_1_4), 1)
          out = F.relu(self.conv1_4(out))
          out_scale_1_4__2D = self.conv_out_scale_1_4(out)

          out_scale_1_4__3D = self.seg_head_1_4(out_scale_1_4__2D) # [1, 20, 16, 128, 128]
          out_scale_1_4__3D = out_scale_1_4__3D.permute(0, 1, 3, 4, 2) # [1, 20, 128, 128, 16]
          return out_scale_1_4__3D

        elif self.out_scale=="1_2":
          # Out 1_4
          out = self.deconv1_8(out_scale_1_8__2D)
          out = torch.cat((out, _skip_1_4), 1)
          out = F.relu(self.conv1_4(out))
          out = self.drop1(out)
          out_scale_1_4__2D = self.conv_out_scale_1_4(out)

          # Out 1_2
          out = self.deconv1_4(out_scale_1_4__2D)
          out = torch.cat((out, _skip_1_2, self.deconv_1_8__1_2(out_scale_1_8__2D)), 1)
          out = F.relu(self.conv1_2(out)) # torch.Size([1, 48, 128, 128])
          out = self.drop2(out)
          out_scale_1_2__2D = self.conv_out_scale_1_2(out) # torch.Size([1, 16, 128, 128])

          out_scale_1_2__3D = self.seg_head_1_2(out_scale_1_2__2D) # [1, 20, 16, 128, 128]
          out_scale_1_2__3D = out_scale_1_2__3D.permute(0, 1, 3, 4, 2) # [1, 20, 128, 128, 16]
          return out_scale_1_2__3D

        elif self.out_scale=="1_1":
          # Out 1_4
          out = self.deconv1_8(out_scale_1_8__2D)
          print('out.shape', out.shape)  # [1, 4, 64, 64]
          out = torch.cat((out, _skip_1_4), 1)
          out = F.relu(self.conv1_4(out))
          out_scale_1_4__2D = self.conv_out_scale_1_4(out)
          # print('out_scale_1_4__2D.shape', out_scale_1_4__2D.shape)  # [1, 8, 64, 64]

          # Out 1_2
          out = self.deconv1_4(out_scale_1_4__2D)
          print('out.shape', out.shape)  # [1, 8, 128, 128]
          out = torch.cat((out, _skip_1_2, self.deconv_1_8__1_2(out_scale_1_8__2D)), 1)
          out = F.relu(self.conv1_2(out)) # torch.Size([1, 48, 128, 128])
          out_scale_1_2__2D = self.conv_out_scale_1_2(out) # torch.Size([1, 16, 128, 128])
          # print('out_scale_1_2__2D.shape', out_scale_1_2__2D.shape)  # [1, 16, 128, 128]

          # Out 1_1
          out = self.deconv1_2(out_scale_1_2__2D)
          out = torch.cat((out, _skip_1_1, self.deconv_1_4__1_1(out_scale_1_4__2D), self.deconv_1_8__1_1(out_scale_1_8__2D)), 1)
          out_scale_1_1__2D = F.relu(self.conv1_1(out)) # [bs, 32, 256, 256]

          out_scale_1_1__3D = self.seg_head_1_1(out_scale_1_1__2D)
          # Take back to [W, H, D] axis order
          out_scale_1_1__3D = out_scale_1_1__3D.permute(0, 1, 3, 4, 2)  # [bs, C, H, W, D] -> [bs, C, W, H, D]
          return out_scale_1_1__3D


    def pack(self, array):
        """ convert a boolean array into a bitwise array. """
        array = array.reshape((-1))

        #compressing bit flags.
        # yapf: disable
        compressed = array[::8] << 7 | array[1::8] << 6  | array[2::8] << 5 | array[3::8] << 4 | array[4::8] << 3 | array[5::8] << 2 | array[6::8] << 1 | array[7::8]
        # yapf: enable

        return np.array(compressed, dtype=np.uint8)

    def forward(self, return_loss=True, **kwargs):
        """Calls either forward_train or forward_test depending on whether
        return_loss=True.
        Note this setting will change the expected inputs. When
        `return_loss=True`, img and img_metas are single-nested (i.e.
        torch.Tensor and list[dict]), and when `resturn_loss=False`, img and
        img_metas should be double nested (i.e.  list[torch.Tensor],
        list[list[dict]]), with the outer list indicating test time
        augmentations.
        """
        if return_loss:
            return self.foward_training(**kwargs)
        else:
            return self.foward_test(**kwargs)


    # @auto_fp16(apply_to=('img', 'points'))
    def foward_training(self,
                        img_metas=None,
                        img=None,
                        target=None):


        # for binary classification
        ones = torch.ones_like(target).to(target.device)
        target = torch.where(torch.logical_or(target==255, target==0), target, ones) # [1, 128, 128, 16]
        # target[target==255] = 2

        len_queue = img.size(1)
        img_metas = [each[len_queue-1] for each in img_metas] # [dict(), dict(), ...] 

        depth =  torch.from_numpy(img_metas[0]["pseudo_pc"]).reshape(256, 256, 32).unsqueeze(0) # [1, 256, 256, 32]
        out_level_1 = self.step(depth.permute(0, 3, 1, 2).to(target.device))

        # calculate loss
        losses = dict()
        losses_pts = dict()
        class_weights_level_1 = self.class_weights_level_1.type_as(target)
        loss_sc_level_1 = BCE_ssc_loss(out_level_1, target, class_weights_level_1, self.alpha)
        losses_pts['loss_sc_level_1'] = loss_sc_level_1

        losses.update(losses_pts)

        return losses

    
    def nll(self, y_pred, target, img_metas):
        cls_prob = y_pred  # Model's predictions
        target = target.cpu().numpy().astype(np.int32)  # Convert target to NumPy array

        # Index arrays for advanced indexing
        batch_index = np.arange(cls_prob.shape[0])[:, None, None, None]
        height_index = np.arange(cls_prob.shape[2])[None, :, None, None]
        width_index = np.arange(cls_prob.shape[3])[None, None, :, None]
        depth_index = np.arange(cls_prob.shape[4])[None, None, None, :]

        mask = target != 255  # Mask to exclude pixels with 255 in target
        target_valid = np.where(mask, target, 0)  # Replace 255 with 0 in target

        # Extract probabilities for the actual classes
        correct_probs = cls_prob[batch_index, target_valid, height_index, width_index, depth_index]
        correct_probs_masked = correct_probs[mask]

        # Calculate Negative Log-Likelihood
        nll = -np.log(correct_probs_masked)
        total_nll = np.sum(nll)
        mean_nll = np.mean(nll)

        # Print mean NLL and frame ID
        # print("\nmean_nll", mean_nll, img_metas[0]["frame_id"])
        return mean_nll


    def crps(self, y_pred, target, img_metas):
        from properscoring import crps_ensemble

        cls_prob = y_pred  # Model's predicted probabilities
        target = target.cpu().numpy().astype(np.int32)  # Convert target to a NumPy array

        num_classes = 2  # Assuming there are 20 classes
        batch_size, _, height, width, depth = cls_prob.shape  # Extract shape information from predictions

        # Create a mask to identify valid pixels (not marked as 255 in the target)
        mask = target != 255

        # Initialize an array for one-hot encoding of the target
        target_one_hot = np.zeros((batch_size, num_classes, height, width, depth))

        # Create one-hot encoding for each valid pixel
        for c in range(num_classes):
            target_one_hot[:, c, :, :, :] = (target == c) & mask

        # Calculate CRPS for the predictions compared to the one-hot encoded target
        # raise NotImplementedError(cls_prob.shape, target_one_hot.shape)
        crps = crps_ensemble(cls_prob, target_one_hot)
        crps = crps.mean()  # Compute the mean CRPS across all pixels
        # print("crps", crps, img_metas[0]["frame_id"])  # Print the mean CRPS and frame ID
        return crps

    def ece(self, y_pred, target, img_metas):
        cls_prob = y_pred  # Model's predicted probabilities
        # Assuming the calibration library 'cal' is correctly imported
        import calibration as cal

        # Create a mask to exclude pixels marked as 255 in the target
        mask = target != 255

        # Flatten the target and apply the mask
        target_flat_masked = target[mask].flatten()

        # Reshape cls_prob and apply the mask, then convert to a NumPy array
        # Reshape to (N, 20), where N = 1*256*256*32 (flattened dimensions of the input)
        cls_prob_reshaped = cls_prob.permute(0, 2, 3, 4, 1).reshape(-1, 2)
        cls_prob_masked_flat = cls_prob_reshaped[mask.view(-1), :].cpu().numpy()

        # Calculate the marginal calibration error
        cls_marginal_calibration_error = cal.get_calibration_error(
            cls_prob_masked_flat, target_flat_masked.cpu().numpy().astype(np.int32))
        # print("\ncls_marginal_calibration_error", cls_marginal_calibration_error, img_metas[0]["frame_id"])  # Print calibration error and frame ID
        return cls_marginal_calibration_error


    def foward_test(self,
                        img_metas=None,
                        sequence_id=None,
                        img=None,
                        target=None,
                        T_velo_2_cam=None,
                        cam_k=None, **kwargs):

        # 07/12/2022, Yiming Li, only support batch_size = 1

        # for binary classification
        ones = torch.ones_like(target).to(target.device)
        target = torch.where(torch.logical_or(target==255, target==0), target, ones) # [1, 128, 128, 16]

        # target[target==255] = 2

        len_queue = img.size(1)
        img_metas = [each[len_queue-1] for each in img_metas] # [dict(), dict(), ...] 

        depth =  torch.from_numpy(img_metas[0]["pseudo_pc"]).reshape(256, 256, 32).unsqueeze(0) # [1, 256, 256, 32] 
        # depth =  torch.from_numpy(img_metas[0]["pseudo_pc"]).reshape(256, 256, 32).unsqueeze(0) # [1, 256, 256, 32]
        ssc_pred = self.step(depth.permute(0, 3, 1, 2).to(target.device))

        y_pred = ssc_pred.detach().cpu().numpy() # [1, 20, 128, 128, 16]
        

        #save query proposal 
        img_path = img_metas[0]['img_filename'] 
        frame_id = os.path.splitext(img_path[0])[0][-6:]
        sequence_id = str(img_metas[0]['sequence_id']).zfill(2)


        # raise NotImplementedError(img_metas[0].keys(),sequence_id)

        #存储结果
        # root="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/deepensemble_qpn/04/"
        root="./deepensemble_qpn/00/"
        os.makedirs(os.path.join(root,sequence_id),exist_ok=True)
        np.save(os.path.join(root,sequence_id,str(frame_id).zfill(8)+".npy"),y_pred)

        #         #读取10个结果
        # root="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/MCDropout_qpn"
        # y_pred_list=[]
        # print(img_metas[0].keys())
        # for i in range(10):
        #     try:
        #         y_pred_tmp = np.load(root+"/"+str(i).zfill(2)+"/"+str(frame_id).zfill(8)+".npy")
        #         y_pred_list.append(y_pred_tmp)
        #     except:
        #         print(root+"/"+str(i).zfill(2)+"/"+str(frame_id).zfill(8)+".npy")
        # #求平均值
        # y_pred=np.mean(y_pred_list,axis=0)
        # y_pred = torch.softmax(torch.from_numpy(y_pred), dim=1).detach().cpu().numpy()
           

        
        # nll=self.nll(y_pred, target, img_metas)
        # crps=self.crps(y_pred, target, img_metas)
        # # ece=self.ece(torch.from_numpy(y_pred).to(ssc_pred.device), 
        # #           target, img_metas)

        #         #nll 和 crps存成numpy
        # # outnp=np.array([nll,crps,ece])
        # outnp=np.array([nll,crps])

        # root="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/uq_out/"
        # os.makedirs(root,exist_ok=True)
        # np.save(root+str(frame_id).zfill(8)+".npy",outnp)


        y_pred = np.argmax(y_pred, axis=1).astype(np.uint8) # [1, 128, 128, 16]

        # y_pred = torch.softmax(torch.from_numpy(y_pred).to(self.bev_embed.weight.device), dim=1).detach().cpu().numpy()
        


        # #nll 和 crps存成numpy
        # outnp=np.array([nll,crps,ece])
        # # outnp=np.array([nll,crps])

        # root="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/uq_out/"
        # os.makedirs(root,exist_ok=True)
        # np.save(root+img_metas[0]["frame_id"]+".npy",outnp)





        # np.savez_compressed(root + img_metas[0]["frame_id"] + ".npz", y_pred=y_pred)

        # msnet3d
        # if not os.path.exists(os.path.join("./kitti/dataset/sequences_msnet3d_sweep10", img_metas[0]['sequence_id'], 'queries')):
            # os.makedirs(os.path.join("./kitti/dataset/sequences_msnet3d_sweep10", img_metas[0]['sequence_id'], 'queries'))
        # save_query_path = os.path.join("./kitti/dataset/sequences_msnet3d_sweep10", img_metas[0]['sequence_id'], 'queries', frame_id + ".query_iou5203_pre7712_rec6153")

        # y_pred_bin = self.pack(y_pred)
        # y_pred_bin.tofile(save_query_path)
        #---------------------------------------------------------------------------------------------------
        
        result = dict()
        y_true = target.cpu().numpy()
        result['y_pred'] = y_pred
        result['y_true'] = y_true
        return result


class SegmentationHead(nn.Module):
  '''
  3D Segmentation heads to retrieve semantic segmentation at each scale.
  Formed by Dim expansion, Conv3D, ASPP block, Conv3D.
  '''
  def __init__(self, inplanes, planes, nbr_classes, dilations_conv_list):
    super().__init__()

    # First convolution
    self.conv0 = nn.Conv3d(inplanes, planes, kernel_size=3, padding=1, stride=1)

    # ASPP Block
    self.conv_list = dilations_conv_list
    self.conv1 = nn.ModuleList(
      [nn.Conv3d(planes, planes, kernel_size=3, padding=dil, dilation=dil, bias=False) for dil in dilations_conv_list])
    self.bn1 = nn.ModuleList([nn.BatchNorm3d(planes) for dil in dilations_conv_list])
    self.conv2 = nn.ModuleList(
      [nn.Conv3d(planes, planes, kernel_size=3, padding=dil, dilation=dil, bias=False) for dil in dilations_conv_list])
    self.bn2 = nn.ModuleList([nn.BatchNorm3d(planes) for dil in dilations_conv_list])
    self.relu = nn.ReLU(inplace=True)

    # Convolution for output
    self.conv_classes = nn.Conv3d(planes, nbr_classes, kernel_size=3, padding=1, stride=1)

  def forward(self, x_in):

    # Dimension exapension
    x_in = x_in[:, None, :, :, :]

    # Convolution to go from inplanes to planes features...
    x_in = self.relu(self.conv0(x_in))

    y = self.bn2[0](self.conv2[0](self.relu(self.bn1[0](self.conv1[0](x_in)))))
    for i in range(1, len(self.conv_list)):
      y += self.bn2[i](self.conv2[i](self.relu(self.bn1[i](self.conv1[i](x_in)))))
    x_in = self.relu(y + x_in)  # modified

    x_in = self.conv_classes(x_in)

    return x_in
