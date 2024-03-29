import torch, time
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
from collections import Counter

from config import *
from utils.processor_utils import Metrics


def config_for_model(args, backbone):
    args.model['epsilon'] = 5.0
    args.model['scl_temperature'] = 0.3

    if 'scl' not in args.model:
        args.model['scl'] = 1
    if  'seel' not in args.model:
        args.model['seel'] = 1

    return args

def import_model(args, backbone):
    # 1. 配置框架参数
    args = config_for_model(args, backbone)

    # 2. 加载框架 + backbone
    seel = SEEL(args, backbone)

    return seel

def scl(embedding, label, temp):
    """calculate the contrastive loss (optimized)"""
    # cosine similarity between embeddings
    cosine_sim = F.cosine_similarity(embedding.unsqueeze(1), embedding.unsqueeze(0), dim=-1) / temp
    # remove diagonal elements from matrix
    mask = ~torch.eye(cosine_sim.shape[0], dtype=bool,device=cosine_sim.device)
    dis = cosine_sim[mask].reshape(cosine_sim.shape[0], -1)
    # apply exp to elements
    dis_exp = torch.exp(dis)
    cosine_sim_exp = torch.exp(cosine_sim)
    row_sum = dis_exp.sum(dim=1) # calculate row sum
    # Pre-compute label counts
    unique_labels, counts = label.unique(return_counts=True)
    label_count = dict(zip(unique_labels.tolist(), counts.tolist()))

    # calculate contrastive loss
    contrastive_loss = 0
    for i in range(len(embedding)):
        n_i = label_count[label[i].item()] - 1
        mask = (label == label[i]) & (torch.arange(len(embedding),device=embedding.device) != i)
        inner_sum = torch.log(cosine_sim_exp[i][mask] / row_sum[i]).sum()
        contrastive_loss += inner_sum / (-n_i) if n_i != 0 else 0

    return contrastive_loss / len(embedding)

# def scl_old(embedding, label, temp):
#     """calculate the contrastive loss
#     """
#     # cosine similarity between embeddings
#     cosine_sim = F.cosine_similarity(embedding.unsqueeze(1), embedding.unsqueeze(0), dim=-1)
#     # remove diagonal elements from matrix
#     dis = cosine_sim[~torch.eye(cosine_sim.shape[0], dtype=bool)].reshape(cosine_sim.shape[0], -1)
#     # apply temprature to elements
#     dis = dis / temp
#     cosine_sim = cosine_sim / temp
#     # apply exp to elements
#     dis = torch.exp(dis)
#     cosine_sim = torch.exp(cosine_sim)

#     # calculate row sum
#     row_sum = []
#     for i in range(len(embedding)):
#         row_sum.append(sum(dis[i]))
#     # calculate outer sum
#     contrastive_loss = 0
#     for i in range(len(embedding)):
#         n_i = label.tolist().count(label[i]) - 1
#         inner_sum = 0
#         # calculate inner sum
#         for j in range(len(embedding)):
#             if label[i] == label[j] and i != j:
#                 inner_sum = inner_sum + torch.log(cosine_sim[i][j] / row_sum[i])
#         if n_i != 0:
#             contrastive_loss += (inner_sum / (-n_i))
#         else:
#             contrastive_loss += 0
#     return contrastive_loss/len(embedding)

class SEEL(nn.Module):
    def __init__(self, args, backbone) -> None:
        super().__init__()
        self.args = args
        self.backbone = backbone
        self.hidden_dim = backbone.hidden_dim
        self.classifier = backbone.classifier
        self.dropout = backbone.dropout
        self.loss_ce = backbone.loss_ce
        self.metrics = Metrics(args, backbone.dataset)


    def seel(self, clss, labs, logits):
        ## 1. 采样 target
        labs_ = labs.detach().cpu().numpy()
        counter = Counter(labs_ )
        prob = {k: max(counter.values())/v for k,v in counter.items()}
        prob = {k: v/sum(prob.values()) for k,v in prob.items()} # 各类类别的采样概率
        target_prob = np.array([prob[l]/counter[l] for l in labs_]) # target 采样概率
        target_idx = np.random.choice(np.arange(len(labs_)), p=target_prob, size=len(labs_))
        target_labs = torch.stack([labs[i] for i in target_idx]) 

        ## 2. 采样 canditate
        one_hot_labs = F.one_hot(labs, num_classes=self.backbone.dataset.n_class).bool()
        prob_idx = F.softmax(logits, dim=-1)[one_hot_labs].detach().cpu().numpy() # batch 中每个样本的正确预测值 (视为fit)
        candidate_prob =  prob_idx / prob_idx.sum() # candidate 采样概率
        candidate_idx = np.random.choice(np.arange(len(labs_)), p=candidate_prob, size=len(target_idx)) # 比重选择
        mark = candidate_idx==target_idx # 确保采样不同个体        
        while True in mark:
            candidate_idx[mark] = np.random.choice(np.arange(len(labs_)), p=candidate_prob, size=sum(mark))
            mark = candidate_idx==target_idx

        ## 3. 判断标签是否相同, 确定系数: 相同(-1,1), 不同(-0.5,0.5)
        t_lab, c_lab = labs_[target_idx], labs_[candidate_idx]
        coeff = torch.cat([torch.rand(1, clss.shape[-1])*2-1 if tl == cl else torch.rand(1, clss.shape[-1])-0.5 for tl, cl in zip(t_lab, c_lab)]).type_as(clss)

        ## 4. OFA 扰动
        target, candidate = clss[target_idx], clss[candidate_idx]
        perturbation = target + coeff*(target-candidate)
        
        ## 5. 扰动损失
        logits = self.classifier(self.dropout(perturbation))       
        loss = self.loss_ce(logits, target_labs)

        return loss

    def forward(self, inputs, stage='train'):
        features = self.backbone.encode(inputs, ['cls'])
        logits = self.classifier(features)
        loss_ce = self.loss_ce(logits, inputs['label'])
        
        if stage == 'train':
            if self.args.model['scl']: # 需要保证每个lab有2个以上样本
                loss_scl = scl(features, inputs['label'], self.args.model['scl_temperature'])
            else: loss_scl = 0
        
            if self.args.model['seel']: 
                loss_seel = self.seel(features, inputs['label'], logits)
            else: loss_seel = 0

        sr = 0.5
        if stage == 'train' and self.args.model['scl'] and self.args.model['seel']: # seel
            loss = (loss_ce*0.1 + loss_scl*0.9)*(1-sr) + loss_seel*sr

        elif stage == 'train' and self.args.model['scl'] and not self.args.model['seel']: # scl
            loss = loss_ce*0.1 + loss_scl*0.9
        else: loss = loss_ce # ce

        # 记录 batch 过程
        labels, preds = inputs['label'].detach().cpu().numpy(), torch.argmax(logits, dim=-1).cpu().numpy()
        if stage == 'train': record = self.metrics.train
        if stage == 'valid': record = self.metrics.valid
        if stage == 'test':  record = self.metrics.test
        record['loss_bz'].append(loss_ce.item())
        record['f1_bz'].append(self.metrics.f1_score(labels, preds))
        record['acc_bz'].append(self.metrics.accuracy_score(labels, preds))

        return {
            'cls': features.detach().cpu().numpy(),
            'loss':   loss,
            'loss_ce': loss_ce,
            'logits': logits,
            'labels': labels,
            'preds':  preds,
        }