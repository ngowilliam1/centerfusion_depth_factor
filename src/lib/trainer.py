from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import torch
import numpy as np
from progress.bar import Bar

from model.data_parallel import DataParallel
from utils.utils import AverageMeter

from model.losses import FastFocalLoss, RegWeightedL1Loss
from model.losses import BinRotLoss, WeightedBCELoss
from model.decode import fusion_decode
from model.utils import _sigmoid, flip_tensor, flip_lr_off, flip_lr, _tranpose_and_gather_feat
from utils.debugger import Debugger
from utils.post_process import generic_post_process
from model.losses import DepthLoss
from utils.pointcloud import generate_pc_hm
import torch.nn.functional as F
import cv2

def get_alpha(rot):
  # output: (B, 8) [bin1_cls[0], bin1_cls[1], bin1_sin, bin1_cos, 
  #                 bin2_cls[0], bin2_cls[1], bin2_sin, bin2_cos]
  # return rot[:, 0]
  idx = rot[:, 1] > rot[:, 5]
  alpha1 = torch.atan2(rot[:, 2], rot[:, 3]) + (-0.5 * np.pi)
  alpha2 = torch.atan2(rot[:, 6], rot[:, 7]) + ( 0.5 * np.pi)
  if idx:
    return alpha1
  return alpha2

def get_alpha_gt(sin1, cos1):
  return torch.atan2(sin1, cos1) + (-0.5 * np.pi)

def apply_transformation_matrix(theta, original_points):
  c, s = torch.cos(theta), torch.sin(theta)
  R = torch.tensor([[c, -s], [s, c]], device=original_points.device)
  return (R @ original_points.T).T

def calculate_vertex(i, j, out, alpha, dim):
  out[i, j, 0, 0] = -dim[i, j, 1]/2
  out[i, j, 0, 1] = -dim[i, j, 2]/2
  out[i, j, 0, 2] = dim[i, j, 0]/2

  out[i, j, 1, 0] = dim[i, j, 1]/2
  out[i, j, 1, 1] = -dim[i, j, 2]/2
  out[i, j, 1, 2] = dim[i, j, 0]/2

  out[i, j, 2, 0] = dim[i, j, 1]/2
  out[i, j, 2, 1] = -dim[i, j, 2]/2
  out[i, j, 2, 2] = -dim[i, j, 0]/2

  out[i, j, 3, 0] = -dim[i, j, 1]/2
  out[i, j, 3, 1] = -dim[i, j, 2]/2
  out[i, j, 3, 2] = -dim[i, j, 0]/2

  out[i, j, 4, 0] = -dim[i, j, 1]/2
  out[i, j, 4, 1] = dim[i, j, 2]/2
  out[i, j, 4, 2] = dim[i, j, 0]/2

  out[i, j, 5, 0] = dim[i, j, 1]/2
  out[i, j, 5, 1] = dim[i, j, 2]/2
  out[i, j, 5, 2] = dim[i, j, 0]/2

  out[i, j, 6, 0] = dim[i, j, 1]/2
  out[i, j, 6, 1] = dim[i, j, 2]/2
  out[i, j, 6, 2] = -dim[i, j, 0]/2

  out[i, j, 7, 0] = -dim[i, j, 1]/2
  out[i, j, 7, 1] = dim[i, j, 2]/2
  out[i, j, 7, 2] = -dim[i, j, 0]/2

  for k in range(8):
    out[i, j, k, 0:2] = apply_transformation_matrix(alpha, out[i, j, k, 0:2])

def get_vertex_loss(dim_output, dim_mask, ind, dim_target, rot_output, rot_mask, rotbin, rotres):
  batch = dim_output.shape[0]
  K = rot_mask.shape[1]

  dim_pred = _tranpose_and_gather_feat(dim_output, ind)*dim_mask
  dim_targeted = dim_target*dim_mask
  
  a = _tranpose_and_gather_feat(rot_output, ind).view(batch, K, -1)
  final_vertex_pred = torch.zeros(size=(batch, K, 8, 3), dtype=torch.float32, layout=torch.strided, device=rot_output.device)
  final_vertex_gt = torch.zeros(size=(batch, K, 8, 3), dtype=torch.float32, layout=torch.strided, device=rot_output.device)
  for i in range(batch):
    for j in rot_mask[i,:].nonzero():
      if rotbin[i, j, 0] == 1 or rotbin[i, j, 1] == 1:
        binID = 0 if rotbin[i, j, 0] == 1 else 1
        gt_alpha = get_alpha_gt(torch.sin(rotres[i, int(j), binID]), torch.cos(rotres[i, int(j), binID]))
        predicted_alpha = get_alpha(a[i][int(j):int(j)+1])[0]
        
        # Calculate location of 8 predicted box vertices with rotation
        calculate_vertex(i, j, final_vertex_pred, predicted_alpha, dim_pred)
        # Calculate location of 8 gt box vertices with rotation
        calculate_vertex(i, j, final_vertex_gt, gt_alpha, dim_targeted)
        
  loss = F.l1_loss(final_vertex_pred, final_vertex_gt, reduction='sum')
  loss = loss / (dim_mask.sum() + 1e-4)      
  return loss
class GenericLoss(torch.nn.Module):
  def __init__(self, opt):
    super(GenericLoss, self).__init__()
    self.crit = FastFocalLoss(opt=opt)
    self.crit_reg = RegWeightedL1Loss()
    if 'rot' in opt.heads:
      self.crit_rot = BinRotLoss()
    if 'nuscenes_att' in opt.heads:
      self.crit_nuscenes_att = WeightedBCELoss()
    self.opt = opt
    self.crit_dep = DepthLoss()
    self.penalize_vertex_loss = opt.penalize_vertex_loss
    self.depth_adjustment_type = opt.depth_adjustment_type
    self.m = opt.m
    self.factor = opt.factor

  def _sigmoid_output(self, output):
    if 'hm' in output:
      output['hm'] = _sigmoid(output['hm'])
    if 'hm_hp' in output:
      output['hm_hp'] = _sigmoid(output['hm_hp'])
    if 'dep' in output:
      output['dep'] = 1. / (output['dep'].sigmoid() + 1e-6) - 1.
    if 'dep_sec' in output and self.opt.sigmoid_dep_sec:
      output['dep_sec'] = 1. / (output['dep_sec'].sigmoid() + 1e-6) - 1.
    return output

  def forward(self, outputs, batch):
    opt = self.opt
    losses = {head: 0 for head in opt.heads}
    for s in range(opt.num_stacks):
      output = outputs[s]
      
      output = self._sigmoid_output(output)
      # HM is heatmap
      if 'hm' in output:
        losses['hm'] += self.crit(
          output['hm'], batch['hm'], batch['ind'], 
          batch['mask'], batch['cat']) / opt.num_stacks
      
      
      if 'dep' in output:
        losses['dep'] += self.crit_dep(
          output['dep'], batch['dep'], batch['ind'], 
          batch['dep_mask'], batch['cat'], self.depth_adjustment_type, batch['dep'], batch['dep_mask'], self.m, self.factor) / opt.num_stacks
      
      regression_heads = [
        'reg', 'wh', 'tracking', 'ltrb', 'ltrb_amodal', 'hps', 
        'dim', 'amodel_offset', 'velocity']

      for head in regression_heads:
        if head in output:
          losses[head] += self.crit_reg(
            output[head], batch[head + '_mask'],
            batch['ind'], batch[head], self.depth_adjustment_type, batch['dep'], batch['dep_mask'], self.m, self.factor) / opt.num_stacks

      # not used
      if 'hm_hp' in output:
        losses['hm_hp'] += self.crit(
          output['hm_hp'], batch['hm_hp'], batch['hp_ind'], 
          batch['hm_hp_mask'], batch['joint']) / opt.num_stacks
        if 'hp_offset' in output:
          losses['hp_offset'] += self.crit_reg(
            output['hp_offset'], batch['hp_offset_mask'],
            batch['hp_ind'], batch['hp_offset']) / opt.num_stacks
        
      if 'rot' in output:
        losses['rot'] += self.crit_rot(
          output['rot'], batch['rot_mask'], batch['ind'], batch['rotbin'],
          batch['rotres']) / opt.num_stacks
      if 'nuscenes_att' in output:
        losses['nuscenes_att'] += self.crit_nuscenes_att(
          output['nuscenes_att'], batch['nuscenes_att_mask'],
          batch['ind'], batch['nuscenes_att']) / opt.num_stacks

      #dep_sec = depth residuals
      if 'dep_sec' in output:
        losses['dep_sec'] += self.crit_dep(
          output['dep_sec'], batch['dep'], batch['ind'], 
          batch['dep_mask'], batch['cat'], self.depth_adjustment_type, batch['dep'], batch['dep_mask'], self.m, self.factor) / opt.num_stacks
      # rot will be set to rot_sec
      if 'rot_sec' in output:
        losses['rot_sec'] += self.crit_rot(
          output['rot_sec'], batch['rot_mask'], batch['ind'], batch['rotbin'],
          batch['rotres']) / opt.num_stacks
    
      if self.penalize_vertex_loss:
        vertex_loss = get_vertex_loss(output['dim'], batch['dim_mask'],
            batch['ind'], batch['dim'], output['rot_sec'], batch['rot_mask'], batch['rotbin'], batch['rotres']) / opt.num_stacks
        
    losses['tot'] = 0
    for head in opt.heads:
      losses['tot'] += opt.weights[head] * losses[head]
    
    if self.penalize_vertex_loss:
      # 8 vertices per box
      losses['vertex_loss'] = 0.125*vertex_loss
      losses['tot'] += losses['vertex_loss']
    return losses['tot'], losses


class ModelWithLoss(torch.nn.Module):
  def __init__(self, model, loss, opt):
    super(ModelWithLoss, self).__init__()
    self.opt = opt
    self.model = model
    self.loss = loss
  
  def forward(self, batch, phase):
    pc_dep = batch.get('pc_dep', None)
    pc_hm = batch.get('pc_hm', None)
    calib = batch['calib'].squeeze(0)

    ## run the first stage
    outputs = self.model(batch['image'], pc_hm=pc_hm, pc_dep=pc_dep, calib=calib)
    # Backprop uses losses['tot']
    loss, loss_stats = self.loss(outputs, batch)
    return outputs[-1], loss, loss_stats


class Trainer(object):
  def __init__(
    self, opt, model, optimizer=None):
    self.opt = opt
    self.optimizer = optimizer
    self.loss_stats, self.loss = self._get_losses(opt)
    
    self.model_with_loss = ModelWithLoss(model, self.loss, opt)
    
  def set_device(self, gpus, chunk_sizes, device):
    if len(gpus) > 1:
      self.model_with_loss = DataParallel(
        self.model_with_loss, device_ids=gpus, 
        chunk_sizes=chunk_sizes).to(device)
    else:
      self.model_with_loss = self.model_with_loss.to(device)
    
    for state in self.optimizer.state.values():
      for k, v in state.items():
        if isinstance(v, torch.Tensor):
          state[k] = v.to(device=device, non_blocking=True)

  def run_epoch(self, phase, epoch, data_loader):
    model_with_loss = self.model_with_loss
    if phase == 'train':
      model_with_loss.train()
    else:
      if len(self.opt.gpus) > 1:
        model_with_loss = self.model_with_loss.module
      model_with_loss.eval()
      torch.cuda.empty_cache()

    opt = self.opt
    results = {}
    data_time, batch_time = AverageMeter(), AverageMeter()
    avg_loss_stats = {l: AverageMeter() for l in self.loss_stats \
                      if l == 'tot' or l == 'vertex_loss' or opt.weights[l] > 0}
    num_iters = len(data_loader) if opt.num_iters < 0 else opt.num_iters
    bar = Bar('{}/{}'.format(opt.task, opt.exp_id), max=num_iters)
    end = time.time()
    
    for iter_id, batch in enumerate(data_loader):
      if iter_id >= num_iters:
        break
      data_time.update(time.time() - end)
      for k in batch:
        if k != 'meta':
          batch[k] = batch[k].to(device=opt.device, non_blocking=True)  
      
      # run one iteration 
      output, loss, loss_stats = model_with_loss(batch, phase)
      
      # backpropagate and step optimizer
      loss = loss.mean()
      if phase == 'train':
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
      batch_time.update(time.time() - end)
      end = time.time()

      Bar.suffix = '{phase}: [{0}][{1}/{2}]|Tot: {total:} |ETA: {eta:} '.format(
        epoch, iter_id, num_iters, phase=phase,
        total=bar.elapsed_td, eta=bar.eta_td)
      for l in avg_loss_stats:
        avg_loss_stats[l].update(
          loss_stats[l].mean().item(), batch['image'].size(0))
        Bar.suffix = Bar.suffix + '|{} {:.4f} '.format(l, avg_loss_stats[l].avg)
      Bar.suffix = Bar.suffix + '|Data {dt.val:.3f}s({dt.avg:.3f}s) ' \
        '|Net {bt.avg:.3f}s'.format(dt=data_time, bt=batch_time)
      if opt.print_iter > 0: # If not using progress bar
        if iter_id % opt.print_iter == 0:
          print('{}/{}| {}'.format(opt.task, opt.exp_id, Bar.suffix)) 
      else:
        bar.next()
      
      if opt.debug > 0:
        self.debug(batch, output, iter_id, dataset=data_loader.dataset)
      
      # generate detections for evaluation
      if (phase == 'val' and (opt.run_dataset_eval or opt.eval)):
        meta = batch['meta']
        dets = fusion_decode(output, K=opt.K, opt=opt)

        for k in dets:
          dets[k] = dets[k].detach().cpu().numpy()

        calib = meta['calib'].detach().numpy() if 'calib' in meta else None
        dets = generic_post_process(opt, dets, 
          meta['c'].cpu().numpy(), meta['s'].cpu().numpy(),
          output['hm'].shape[2], output['hm'].shape[3], self.opt.num_classes,
          calib)

        # merge results
        result = []
        for i in range(len(dets[0])):
          if dets[0][i]['score'] > self.opt.out_thresh and all(dets[0][i]['dim'] > 0):
            result.append(dets[0][i])

        img_id = batch['meta']['img_id'].numpy().astype(np.int32)[0]
        results[img_id] = result
 
      del output, loss, loss_stats
    
    bar.finish()
    ret = {k: v.avg for k, v in avg_loss_stats.items()}
    ret['time'] = bar.elapsed_td.total_seconds() / 60.
    return ret, results


  def _get_losses(self, opt):
    loss_order = ['hm', 'wh', 'reg', 'ltrb', 'hps', 'hm_hp', \
      'hp_offset', 'dep', 'dep_sec', 'dim', 'rot', 'rot_sec',
      'amodel_offset', 'ltrb_amodal', 'tracking', 'nuscenes_att', 'velocity']
    loss_states = ['tot'] + [k for k in loss_order if k in opt.heads]
    if opt.penalize_vertex_loss:
      loss_order += ['vertex_loss']
      loss_states += ['vertex_loss']
    loss = GenericLoss(opt)
    return loss_states, loss


  def debug(self, batch, output, iter_id, dataset):
    opt = self.opt
    if 'pre_hm' in batch:
      output.update({'pre_hm': batch['pre_hm']})
    dets = fusion_decode(output, K=opt.K, opt=opt)
    for k in dets:
      dets[k] = dets[k].detach().cpu().numpy()
    dets_gt = batch['meta']['gt_det']
    for i in range(1):
      debugger = Debugger(opt=opt, dataset=dataset)
      img = batch['image'][i].detach().cpu().numpy().transpose(1, 2, 0)
      img = np.clip(((
        img * dataset.std + dataset.mean) * 255.), 0, 255).astype(np.uint8)
      pred = debugger.gen_colormap(output['hm'][i].detach().cpu().numpy())
      gt = debugger.gen_colormap(batch['hm'][i].detach().cpu().numpy())
      debugger.add_blend_img(img, pred, 'pred_hm', trans=self.opt.hm_transparency)
      debugger.add_blend_img(img, gt, 'gt_hm', trans=self.opt.hm_transparency)
      
      debugger.add_img(img, img_id='img')
      
      # show point clouds
      if opt.pointcloud:
        pc_2d = batch['pc_2d'][i].detach().cpu().numpy()
        pc_3d = None
        pc_N = batch['pc_N'][i].detach().cpu().numpy()
        debugger.add_img(img, img_id='pc')
        debugger.add_pointcloud(pc_2d, pc_N, img_id='pc')
        
        if 'pc_hm' in opt.pc_feat_lvl:
          channel = opt.pc_feat_channels['pc_hm']
          pc_hm = debugger.gen_colormap(batch['pc_hm'][i][channel].unsqueeze(0).detach().cpu().numpy())
          debugger.add_blend_img(img, pc_hm, 'pc_hm', trans=self.opt.hm_transparency)
        if 'pc_dep' in opt.pc_feat_lvl:
          channel = opt.pc_feat_channels['pc_dep']
          pc_hm = batch['pc_hm'][i][channel].unsqueeze(0).detach().cpu().numpy()
          pc_dep = debugger.add_overlay_img(img, pc_hm, 'pc_dep')
          

      if 'pre_img' in batch:
        pre_img = batch['pre_img'][i].detach().cpu().numpy().transpose(1, 2, 0)
        pre_img = np.clip(((
          pre_img * dataset.std + dataset.mean) * 255), 0, 255).astype(np.uint8)
        debugger.add_img(pre_img, 'pre_img_pred')
        debugger.add_img(pre_img, 'pre_img_gt')
        if 'pre_hm' in batch:
          pre_hm = debugger.gen_colormap(
            batch['pre_hm'][i].detach().cpu().numpy())
          debugger.add_blend_img(pre_img, pre_hm, 'pre_hm', trans=self.opt.hm_transparency)

      debugger.add_img(img, img_id='out_pred')
      if 'ltrb_amodal' in opt.heads:
        debugger.add_img(img, img_id='out_pred_amodal')
        debugger.add_img(img, img_id='out_gt_amodal')

      # Predictions
      for k in range(len(dets['scores'][i])):
        if dets['scores'][i, k] > opt.vis_thresh:
          debugger.add_coco_bbox(
            dets['bboxes'][i, k] * opt.down_ratio, dets['clses'][i, k],
            dets['scores'][i, k], img_id='out_pred')

          if 'ltrb_amodal' in opt.heads:
            debugger.add_coco_bbox(
              dets['bboxes_amodal'][i, k] * opt.down_ratio, dets['clses'][i, k],
              dets['scores'][i, k], img_id='out_pred_amodal')

          if 'hps' in opt.heads and int(dets['clses'][i, k]) == 0:
            debugger.add_coco_hp(
              dets['hps'][i, k] * opt.down_ratio, img_id='out_pred')

          if 'tracking' in opt.heads:
            debugger.add_arrow(
              dets['cts'][i][k] * opt.down_ratio, 
              dets['tracking'][i][k] * opt.down_ratio, img_id='out_pred')
            debugger.add_arrow(
              dets['cts'][i][k] * opt.down_ratio, 
              dets['tracking'][i][k] * opt.down_ratio, img_id='pre_img_pred')

      # Ground truth
      debugger.add_img(img, img_id='out_gt')
      for k in range(len(dets_gt['scores'][i])):
        if dets_gt['scores'][i][k] > opt.vis_thresh:
          if 'dep' in dets_gt.keys():
            dist = dets_gt['dep'][i][k]
            if len(dist)>1:
              dist = dist[0]
          else:
            dist = -1
          debugger.add_coco_bbox(
            dets_gt['bboxes'][i][k] * opt.down_ratio, dets_gt['clses'][i][k],
            dets_gt['scores'][i][k], img_id='out_gt', dist=dist)

          if 'ltrb_amodal' in opt.heads:
            debugger.add_coco_bbox(
              dets_gt['bboxes_amodal'][i, k] * opt.down_ratio, 
              dets_gt['clses'][i, k],
              dets_gt['scores'][i, k], img_id='out_gt_amodal')

          if 'hps' in opt.heads and \
            (int(dets['clses'][i, k]) == 0):
            debugger.add_coco_hp(
              dets_gt['hps'][i][k] * opt.down_ratio, img_id='out_gt')

          if 'tracking' in opt.heads:
            debugger.add_arrow(
              dets_gt['cts'][i][k] * opt.down_ratio, 
              dets_gt['tracking'][i][k] * opt.down_ratio, img_id='out_gt')
            debugger.add_arrow(
              dets_gt['cts'][i][k] * opt.down_ratio, 
              dets_gt['tracking'][i][k] * opt.down_ratio, img_id='pre_img_gt')

      if 'hm_hp' in opt.heads:
        pred = debugger.gen_colormap_hp(
          output['hm_hp'][i].detach().cpu().numpy())
        gt = debugger.gen_colormap_hp(batch['hm_hp'][i].detach().cpu().numpy())
        debugger.add_blend_img(img, pred, 'pred_hmhp', trans=self.opt.hm_transparency)
        debugger.add_blend_img(img, gt, 'gt_hmhp', trans=self.opt.hm_transparency)


      if 'rot' in opt.heads and 'dim' in opt.heads and 'dep' in opt.heads:
        dets_gt = {k: dets_gt[k].cpu().numpy() for k in dets_gt}
        calib = batch['meta']['calib'].detach().numpy() \
                if 'calib' in batch['meta'] else None
        det_pred = generic_post_process(opt, dets, 
          batch['meta']['c'].cpu().numpy(), batch['meta']['s'].cpu().numpy(),
          output['hm'].shape[2], output['hm'].shape[3], self.opt.num_classes,
          calib)
        det_gt = generic_post_process(opt, dets_gt, 
          batch['meta']['c'].cpu().numpy(), batch['meta']['s'].cpu().numpy(),
          output['hm'].shape[2], output['hm'].shape[3], self.opt.num_classes,
          calib, is_gt=True)

        debugger.add_3d_detection(
          batch['meta']['img_path'][i], batch['meta']['flipped'][i],
          det_pred[i], calib[i],
          vis_thresh=opt.vis_thresh, img_id='add_pred')
        debugger.add_3d_detection(
          batch['meta']['img_path'][i], batch['meta']['flipped'][i], 
          det_gt[i], calib[i],
          vis_thresh=opt.vis_thresh, img_id='add_gt')
        
        pc_3d = None
        if opt.pointcloud:
          pc_3d=batch['pc_3d'].cpu().numpy()

        debugger.add_bird_views(det_pred[i], det_gt[i], vis_thresh=opt.vis_thresh, 
          img_id='bird_pred_gt', pc_3d=pc_3d, show_velocity=opt.show_velocity)
        debugger.add_bird_views([], det_gt[i], vis_thresh=opt.vis_thresh, 
          img_id='bird_gt', pc_3d=pc_3d, show_velocity=opt.show_velocity)

      if opt.debug == 4:
        debugger.save_all_imgs(opt.debug_dir, prefix='{}'.format(iter_id))
      else:
        debugger.show_all_imgs(pause=True)
  
  def val(self, epoch, data_loader):
    return self.run_epoch('val', epoch, data_loader)

  def train(self, epoch, data_loader):
    return self.run_epoch('train', epoch, data_loader)
