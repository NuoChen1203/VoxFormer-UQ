# Copyright (c) 2022-2023, NVIDIA Corporation & Affiliates. All rights reserved.
#
# This work is made available under the Nvidia Source Code License-NC.
# To view a copy of this license, visit
# https://github.com/NVlabs/VoxFormer/blob/main/LICENSE

# ---------------------------------------------
# Copyright (c) OpenMMLab. All rights reserved.
# ---------------------------------------------
#  Modified by Zhiqi Li
# ---------------------------------------------

import os
import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from einops.layers.torch import Rearrange
from mmdet.models import HEADS
from mmdet.models.utils import build_transformer
from mmdet.core import (multi_apply, multi_apply, reduce_mean)
from mmcv.cnn.bricks.transformer import build_positional_encoding
from projects.mmdet3d_plugin.voxformer.utils.header import Header
from projects.mmdet3d_plugin.voxformer.utils.ssc_loss import sem_scal_loss, KL_sep, geo_scal_loss, CE_ssc_loss
from projects.mmdet3d_plugin.models.utils.bricks import run_time

@HEADS.register_module()
class VoxFormerHead(nn.Module):
    def __init__(
        self,
        *args,
        bev_h,
        bev_w,
        bev_z,
        cross_transformer,
        self_transformer,
        positional_encoding,
        embed_dims,
        CE_ssc_loss=True,
        geo_scal_loss=True,
        sem_scal_loss=True,
        save_flag = False,
        **kwargs
    ):
        super().__init__()
        self.bev_h = bev_h
        self.bev_w = bev_w 
        self.bev_z = bev_z
        self.real_w = 51.2
        self.real_h = 51.2
        self.n_classes = 20
        self.embed_dims = embed_dims
        self.bev_embed = nn.Embedding((self.bev_h) * (self.bev_w) * (self.bev_z), self.embed_dims)
        self.mask_embed = nn.Embedding(1, self.embed_dims)
        self.positional_encoding = build_positional_encoding(positional_encoding)
        self.cross_transformer = build_transformer(cross_transformer)
        self.self_transformer = build_transformer(self_transformer)
        self.header = Header(self.n_classes, nn.BatchNorm3d, feature=self.embed_dims)
        self.class_names =  [ "empty", "car", "bicycle", "motorcycle", "truck", "other-vehicle", "person", "bicyclist", "motorcyclist", "road", 
                            "parking", "sidewalk", "other-ground", "building", "fence", "vegetation", "trunk", "terrain", "pole", "traffic-sign",]
        self.class_weights = torch.from_numpy(np.array([0.446, 0.603, 0.852, 0.856, 0.747, 0.734, 0.801, 0.796, 0.818, 0.557, 
                                                        0.653, 0.568, 0.683, 0.560, 0.603, 0.530, 0.688, 0.574, 0.716, 0.786]))
        self.CE_ssc_loss = CE_ssc_loss
        self.sem_scal_loss = sem_scal_loss
        self.geo_scal_loss = geo_scal_loss
        self.save_flag = save_flag
        
    def forward(self, mlvl_feats, img_metas, target):
        """Forward function.
        Args:
            mlvl_feats (tuple[Tensor]): Features from the upstream
                network, each is a 5D-tensor with shape
                (B, N, C, H, W).
            img_metas: Meta information such as camera intrinsics.
            target: Semantic completion ground truth. 
        Returns:
            ssc_logit (Tensor): Outputs from the segmentation head.
        """

        bs, num_cam, _, _, _ = mlvl_feats[0].shape
        dtype = mlvl_feats[0].dtype
        bev_queries = self.bev_embed.weight.to(dtype) #[128*128*16, dim]

        # Generate bev postional embeddings for cross and self attention
        bev_pos_cross_attn = self.positional_encoding(torch.zeros((bs, 512, 512), device=bev_queries.device).to(dtype)).to(dtype) # [1, dim, 128*4, 128*4]
        bev_pos_self_attn = self.positional_encoding(torch.zeros((bs, 512, 512), device=bev_queries.device).to(dtype)).to(dtype) # [1, dim, 128*4, 128*4]

        # Load query proposals
        proposal =  img_metas[0]['proposal'].reshape(self.bev_h, self.bev_w, self.bev_z)




        frame_id=img_metas[0]["frame_id"]
        root="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/deepensemble_qpn"
        y_pred_list=[]
        # print(img_metas[0].keys())
        for i in range(5):
            try:
                y_pred_tmp = np.load(os.path.join(root,str(i).zfill(2),img_metas[0]["sequence_id"],str(frame_id).zfill(8)+".npy"))
                y_pred_list.append(y_pred_tmp)
            except:
                print(os.path.join(root,str(i).zfill(2),img_metas[0]["sequence_id"],str(frame_id).zfill(8)+".npy"))
        #求平均值
        y_pred=np.mean(y_pred_list,axis=0)
        y_pred = torch.softmax(torch.from_numpy(y_pred), dim=1).detach().cpu().numpy()

        y = np.argmax(y_pred, axis=1).astype(np.uint8) # [1, 128, 128, 16]
        p=y_pred[:,1:2,...].reshape(self.bev_h, self.bev_w, self.bev_z)


        # hp=-p * torch.log(p) - (1 - p) * torch.log(1 - p)
        hp=-p*np.log(p)-(1-p)*np.log(1-p)

        y=y.reshape(self.bev_h, self.bev_w, self.bev_z)

        #y=1的地方prob=1-0.5*hp，否则prob=0.5*hp
        prob=np.where(y==1,1-0.5*hp,0.5*hp)




        
        prob = torch.tensor(prob, device=self.bev_embed.weight.device).to(dtype)
        prob = prob.unsqueeze(-1)
        bev_queries = prob * bev_queries.reshape(self.bev_h, self.bev_w, self.bev_z, self.embed_dims) 
        bev_queries = bev_queries.reshape(-1, self.embed_dims)

        proposal=np.ones_like(proposal)
        unmasked_idx = np.asarray(np.where(proposal.reshape(-1)>0)).astype(np.int32)
        masked_idx = np.asarray(np.where(proposal.reshape(-1)==0)).astype(np.int32)
        vox_coords, ref_3d = self.get_ref_3d()

        # Compute seed features of query proposals by deformable cross attention
        seed_feats = self.cross_transformer.get_vox_features(
            mlvl_feats, 
            bev_queries,
            self.bev_h,
            self.bev_w,
            ref_3d=ref_3d,
            vox_coords=vox_coords,
            unmasked_idx=unmasked_idx,
            grid_length=(self.real_h / self.bev_h, self.real_w / self.bev_w),
            bev_pos=bev_pos_cross_attn,
            img_metas=img_metas,
            prev_bev=None,
        )

        # Complete voxel features by adding mask tokens
        vox_feats = torch.empty((self.bev_h, self.bev_w, self.bev_z, self.embed_dims), device=bev_queries.device)
        vox_feats_flatten = vox_feats.reshape(-1, self.embed_dims)
        vox_feats_flatten[vox_coords[unmasked_idx[0], 3], :] = seed_feats[0]
        vox_feats_flatten[vox_coords[masked_idx[0], 3], :] = self.mask_embed.weight.view(1, self.embed_dims).expand(masked_idx.shape[1], self.embed_dims).to(dtype)

        # Diffuse voxel features by deformable self attention
        vox_feats_diff = self.self_transformer.diffuse_vox_features(
            mlvl_feats,
            vox_feats_flatten,
            512,
            512,
            ref_3d=ref_3d,
            vox_coords=vox_coords,
            unmasked_idx=unmasked_idx,
            grid_length=(self.real_h / self.bev_h, self.real_w / self.bev_w),
            bev_pos=bev_pos_self_attn,
            img_metas=img_metas,
            prev_bev=None,
        )
        vox_feats_diff = vox_feats_diff.reshape(self.bev_h, self.bev_w, self.bev_z, self.embed_dims)
        input_dict = {
            "x3d": vox_feats_diff.permute(3, 0, 1, 2).unsqueeze(0),
        }
        out = self.header(input_dict)
        return out 

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

        num_classes = 20  # Assuming there are 20 classes
        batch_size, _, height, width, depth = cls_prob.shape  # Extract shape information from predictions

        # Create a mask to identify valid pixels (not marked as 255 in the target)
        mask = target != 255

        # Initialize an array for one-hot encoding of the target
        target_one_hot = np.zeros((batch_size, num_classes, height, width, depth))

        # Create one-hot encoding for each valid pixel
        for c in range(num_classes):
            target_one_hot[:, c, :, :, :] = (target == c) & mask

        # Calculate CRPS for the predictions compared to the one-hot encoded target
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
        cls_prob_reshaped = cls_prob.permute(0, 2, 3, 4, 1).reshape(-1, 20)
        cls_prob_masked_flat = cls_prob_reshaped[mask.view(-1), :].cpu().numpy()

        # Calculate the marginal calibration error
        cls_marginal_calibration_error = cal.get_calibration_error(
            cls_prob_masked_flat, target_flat_masked.cpu().numpy().astype(np.int32))
        # print("\ncls_marginal_calibration_error", cls_marginal_calibration_error, img_metas[0]["frame_id"])  # Print calibration error and frame ID
        return cls_marginal_calibration_error

    def step(self, out_dict, target, img_metas, step_type):
        """Training/validation function.
        Args:
            out_dict (dict[Tensor]): Segmentation output.
            img_metas: Meta information such as camera intrinsics.
            target: Semantic completion ground truth. 
            step_type: Train or test.
        Returns:
            loss or predictions
        """

        ssc_pred = out_dict["ssc_logit"]

        if step_type== "train":
            loss_dict = dict()

            class_weight = self.class_weights.type_as(target)
            if self.CE_ssc_loss:
                loss_ssc = CE_ssc_loss(ssc_pred, target, class_weight)
                loss_dict['loss_ssc'] = loss_ssc

            if self.sem_scal_loss:
                loss_sem_scal = sem_scal_loss(ssc_pred, target)
                loss_dict['loss_sem_scal'] = loss_sem_scal

            if self.geo_scal_loss:
                loss_geo_scal = geo_scal_loss(ssc_pred, target)
                loss_dict['loss_geo_scal'] = loss_geo_scal

            return loss_dict

        elif step_type== "val" or "test":
            y_true = target.cpu().numpy()
            y_pred = ssc_pred.detach().cpu().numpy()


            





            # #存储结果
            # # raise NotImplementedError(y_pred.shape)
            # # raise NotImplementedError(img_metas[0]["frame_id"])
            # root="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/MCDropout/09/"
            # os.makedirs(root,exist_ok=True)
            # np.save(root+img_metas[0]["frame_id"]+".npy",y_pred)
            # # np.savez_compressed(root + img_metas[0]["frame_id"] + ".npz", y_pred=y_pred)




            # #读取10个结果
            # root="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/MCDropout"
            # y_pred_list=[]
            # print(img_metas[0].keys())
            # for i in range(10):
            #     try:
            #         y_pred_tmp = np.load(root+"/"+str(i).zfill(2)+"/"+img_metas[0]["frame_id"]+".npy")
            #         y_pred_list.append(y_pred_tmp)
            #     except:
            #         print(root+"/"+str(i).zfill(2)+"/"+img_metas[0]["frame_id"]+".npy")
            # #求平均值
            # y_pred=np.mean(y_pred_list,axis=0)

            # y_pred = torch.softmax(torch.from_numpy(y_pred).to(self.bev_embed.weight.device), dim=1).detach().cpu().numpy()
           

            # nll=self.nll(y_pred, target, img_metas)
            # crps=self.crps(y_pred, target, img_metas)
            # ece=self.ece(torch.from_numpy(y_pred).to(ssc_pred.device), 
            #          target, img_metas)

            # #nll 和 crps存成numpy
            # outnp=np.array([nll,crps,ece])
            # # outnp=np.array([nll,crps])

            # root="/root/autodl-tmp/vox/mmdetection3d/VoxFormer-UQ/uq_out/"
            # os.makedirs(root,exist_ok=True)
            # np.save(root+img_metas[0]["frame_id"]+".npy",outnp)


            y_pred = np.argmax(y_pred, axis=1)

            result = dict()
            result['y_pred'] = y_pred
            result['y_true'] = y_true

            if self.save_flag:
                assert False
                self.save_pred(img_metas, y_pred)

            return result

    def training_step(self, out_dict, target, img_metas):
        """Training step.
        """
        return self.step(out_dict, target, img_metas, "train")

    def validation_step(self, out_dict, target, img_metas):
        """Validation step.
        """
        return self.step(out_dict, target, img_metas, "val")

    def get_ref_3d(self):
        """Get reference points in 3D.
        Args:
            self.real_h, self.bev_h
        Returns:
            vox_coords (Array): Voxel indices
            ref_3d (Array): 3D reference points
        """
        scene_size = (51.2, 51.2, 6.4)
        vox_origin = np.array([0, -25.6, -2])
        voxel_size = self.real_h / self.bev_h

        vol_bnds = np.zeros((3,2))
        vol_bnds[:,0] = vox_origin
        vol_bnds[:,1] = vox_origin + np.array(scene_size)

        # Compute the voxels index in lidar cooridnates
        vol_dim = np.ceil((vol_bnds[:,1]- vol_bnds[:,0])/ voxel_size).copy(order='C').astype(int)
        idx = np.array([range(vol_dim[0]*vol_dim[1]*vol_dim[2])])
        xv, yv, zv = np.meshgrid(range(vol_dim[0]), range(vol_dim[1]), range(vol_dim[2]), indexing='ij')
        vox_coords = np.concatenate([xv.reshape(1,-1), yv.reshape(1,-1), zv.reshape(1,-1), idx], axis=0).astype(int).T

        # Normalize the voxels centroids in lidar cooridnates
        ref_3d = np.concatenate([(xv.reshape(1,-1)+0.5)/self.bev_h, (yv.reshape(1,-1)+0.5)/self.bev_w, (zv.reshape(1,-1)+0.5)/self.bev_z,], axis=0).astype(np.float64).T 

        return vox_coords, ref_3d

    def save_pred(self, img_metas, y_pred):
        """Save predictions for evaluations and visualizations.

        learning_map_inv: inverse of previous map
        
        0: 0    # "unlabeled/ignored"  # 1: 10   # "car"        # 2: 11   # "bicycle"       # 3: 15   # "motorcycle"     # 4: 18   # "truck" 
        5: 20   # "other-vehicle"      # 6: 30   # "person"     # 7: 31   # "bicyclist"     # 8: 32   # "motorcyclist"   # 9: 40   # "road"   
        10: 44  # "parking"            # 11: 48  # "sidewalk"   # 12: 49  # "other-ground"  # 13: 50  # "building"       # 14: 51  # "fence"          
        15: 70  # "vegetation"         # 16: 71  # "trunk"      # 17: 72  # "terrain"       # 18: 80  # "pole"           # 19: 81  # "traffic-sign"
        """

        y_pred[y_pred==10] = 44
        y_pred[y_pred==11] = 48
        y_pred[y_pred==12] = 49
        y_pred[y_pred==13] = 50
        y_pred[y_pred==14] = 51
        y_pred[y_pred==15] = 70
        y_pred[y_pred==16] = 71
        y_pred[y_pred==17] = 72
        y_pred[y_pred==18] = 80
        y_pred[y_pred==19] = 81
        y_pred[y_pred==1] = 10
        y_pred[y_pred==2] = 11
        y_pred[y_pred==3] = 15
        y_pred[y_pred==4] = 18
        y_pred[y_pred==5] = 20
        y_pred[y_pred==6] = 30
        y_pred[y_pred==7] = 31
        y_pred[y_pred==8] = 32
        y_pred[y_pred==9] = 40

        # save predictions
        pred_folder = os.path.join("./voxformer", "sequences", img_metas[0]['sequence_id'], "predictions") 
        if not os.path.exists(pred_folder):
            os.makedirs(pred_folder)
        y_pred_bin = y_pred.astype(np.uint16)
        y_pred_bin.tofile(os.path.join(pred_folder, img_metas[0]['frame_id'] + ".label"))
