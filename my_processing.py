import numpy as np
import pandas as pd
import os
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset, TensorDataset, DataLoader
from torch.nn.utils.rnn import pad_sequence
from utils import build_dense_graph
import re
import numpy as np

class KTDataset(Dataset):
    def __init__(self, features, questions, answers):
        super(KTDataset, self).__init__()
        self.features = features
        self.questions = questions
        self.answers = answers

    def __getitem__(self, index):
        return self.features[index], self.questions[index], self.answers[index]

    def __len__(self):
        return len(self.features)


def pad_collate(batch):
    (features, questions, answers) = zip(*batch)
    features = [torch.LongTensor(feat) for feat in features]
    questions = [torch.LongTensor(qt) for qt in questions]
    answers = [torch.LongTensor(ans) for ans in answers]
    feature_pad = pad_sequence(features, batch_first=True, padding_value=-1)
    question_pad = pad_sequence(questions, batch_first=True, padding_value=-1)
    answer_pad = pad_sequence(answers, batch_first=True, padding_value=-1)
    return feature_pad, question_pad, answer_pad


def load_dataset(file_path, batch_size, graph_type, dkt_graph_path=None, train_ratio=0.7, val_ratio=0.2, shuffle=True, model_type='GKT', use_binary=True, res_len=2, use_cuda=True):
    r"""
    Parameters:
        file_path: input file path of knowledge tracing data
        batch_size: the size of a student batch
        graph_type: the type of the concept graph
        shuffle: whether to shuffle the dataset or not
        use_cuda: whether to use GPU to accelerate training speed
    Return:
        concept_num: the number of all concepts(or questions)
        graph: the static graph is graph type is in ['Dense', 'Transition', 'DKT'], otherwise graph is None
        train_data_loader: data loader of the training dataset
        valid_data_loader: data loader of the validation dataset
        test_data_loader: data loader of the test dataset
    NOTE: stole some code from https://github.com/lccasagrande/Deep-Knowledge-Tracing/blob/master/deepkt/data_util.py
    """
    df = pd.read_csv(file_path)
    if "kc_uid" not in df.columns:
        raise KeyError(f"The column 'kc_uid' was not found on {file_path}")
    if "accuracy" not in df.columns:
        raise KeyError(f"The column 'accuracy' was not found on {file_path}")
    if "knowre_user_id" not in df.columns:
        raise KeyError(f"The column 'knowre_user_id' was not found on {file_path}")

    # Step 1.1 - Remove questions without skill
    df.dropna(subset=['kc_uid'], inplace=True)

    # Step 1.2 - Remove users with a single answer
    df = df.groupby('knowre_user_id').filter(lambda q: len(q) > 1).copy()

    # Step 2 - Enumerate skill id
    # ????????? ????????? ?????? ????????? ????????? ?????????
    df['skill'], _ = pd.factorize(df['kc_uid'], sort=True)  # we can also use problem_id to represent exercises

    # Step 3 - Cross skill id with answer to form a synthetic feature
    # use_binary: (0,1); !use_binary: (1,2,3,4,5,6,7,8,9,10,11,12). Either way, the correct result index is guaranteed to be 1
    if use_binary:
        df['skill_with_answer'] = df['skill'] * 2 + df['accuracy']
    else:
        df['skill_with_answer'] = df['skill'] * res_len + df['accuracy'] - 1


    # Step 4 - Convert to a sequence per user id and shift features 1 timestep
    feature_list = []
    question_list = []
    answer_list = []
    seq_len_list = []

    def get_data(series):
        feature_list.append(series['skill_with_answer'].tolist())
        question_list.append(series['skill'].tolist())
        answer_list.append(series['accuracy'].eq(1).astype('int').tolist())
        seq_len_list.append(series['accuracy'].shape[0])

    df.groupby('knowre_user_id').apply(get_data)
    max_seq_len = np.max(seq_len_list)
    print('max seq_len: ', max_seq_len)
    student_num = len(seq_len_list)
    print('student num: ', student_num)
    feature_dim = int(df['skill_with_answer'].max() + 1)
    print('feature_dim: ', feature_dim)
    question_dim = int(df['skill'].max() + 1)
    print('question_dim: ', question_dim)
    concept_num = question_dim

    # print('feature_dim:', feature_dim, 'res_len*question_dim:', res_len*question_dim)
    # assert feature_dim == res_len * question_dim

    kt_dataset = KTDataset(feature_list, question_list, answer_list)
    train_size = int(train_ratio * student_num)
    val_size = int(val_ratio * student_num)
    test_size = student_num - train_size - val_size
    train_dataset, val_dataset, test_dataset = torch.utils.data.random_split(kt_dataset, [train_size, val_size, test_size])
    print('train_size: ', train_size, 'val_size: ', val_size, 'test_size: ', test_size)

    train_data_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=shuffle, collate_fn=pad_collate)
    valid_data_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=shuffle, collate_fn=pad_collate)
    test_data_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=shuffle, collate_fn=pad_collate)

    graph = None
    if model_type == 'GKT':
        if graph_type == 'Dense':
            graph = build_dense_graph(concept_num)
        elif graph_type == 'Transition':
            graph = build_transition_graph(question_list, seq_len_list, train_dataset.indices, student_num, concept_num)
        elif graph_type == 'DKT':
            graph = build_dkt_graph(dkt_graph_path, concept_num)
        elif graph_type == 'MyGraph':
            graph = normed_adj_graph()
        elif graph_type == 'MyHMM':
            graph = normed_adj_hmm_graph()
        elif graph_type == 'MyERF':
            graph = normed_adj_ERF_graph()
        elif graph_type == 'MyFIR':
            graph = normed_adj_FIR_graph()
        elif graph_type == 'My2Hop':
            graph = two_hop_transition_graph(question_list, seq_len_list, train_dataset.indices, student_num, concept_num)
        elif graph_type == 'My2HopD':
            graph = two_hop_transition_daekyo_graph(question_list, seq_len_list, train_dataset.indices, student_num, concept_num)
        if use_cuda and graph_type in ['Dense', 'Transition', 'DKT', 'MyGraph', 'MyHMM', 'MyERF', 'MyFIR', 'My2Hop', 'My2HopD']:
            graph = graph.cuda()
    return concept_num, graph, train_data_loader, valid_data_loader, test_data_loader


def build_transition_graph(question_list, seq_len_list, indices, student_num, concept_num):
    graph = np.zeros((concept_num, concept_num))
    student_dict = dict(zip(indices, np.arange(student_num)))
    for i in range(student_num):
        if i not in student_dict:
            continue
        questions = question_list[i]
        seq_len = seq_len_list[i]
        for j in range(seq_len - 1):
            pre = questions[j]
            next = questions[j + 1]
            graph[pre, next] += 1
    np.fill_diagonal(graph, 0)
    # row normalization
    rowsum = np.array(graph.sum(1))
    def inv(x):
        if x == 0:
            return x
        return 1. / x
    inv_func = np.vectorize(inv)
    r_inv = inv_func(rowsum).flatten()
    r_mat_inv = np.diag(r_inv)
    graph = r_mat_inv.dot(graph)
    # covert to tensor
    graph = torch.from_numpy(graph).float()
    return graph


def build_dkt_graph(file_path, concept_num):
    graph = np.loadtxt(file_path)
    assert graph.shape[0] == concept_num and graph.shape[1] == concept_num
    graph = torch.from_numpy(graph).float()
    return graph


def normed_adj_graph():
    gt = pd.read_csv('./data/GT_SSM11_1116.csv')
    kcs = gt['from'].unique().tolist()
    kcs.extend(gt['to'].unique().tolist())
    kcs = list(set(kcs))
    kcs.sort()
    graph = np.zeros([len(kcs), len(kcs)])
    for i in range(len(gt)):
        graph[kcs.index(gt['from'][i])][kcs.index(gt['to'][i])] = 1
    # row normalization
    for i in range(len(graph)):
        if graph[i].sum() != 0:
            graph[i] = (graph[i]/graph[i].sum())
    # covert to tensor
    graph = torch.from_numpy(graph).float()
    return graph

# HMM
def normed_adj_hmm_graph():
    gt = pd.read_csv('./data/HMM_11.csv', encoding='cp949')
    cond = gt.hmm_direction == 'forward'
    gt = gt[cond]
    gt.reset_index(drop=True, inplace=True)
    gt = gt[['before', 'after']]
    
    KC = pd.read_csv('./data/kc_dedup_smath11.csv')
    kcs = KC['kc_uid'].unique().tolist()
    kcs.sort()

    graph = np.zeros([len(kcs), len(kcs)])

    for i in range(len(gt)):
        graph[kcs.index(gt['before'][i])][kcs.index(gt['after'][i])] = 1
    # row normalization
    for i in range(len(graph)):
        if graph[i].sum() != 0:
            graph[i] = (graph[i]/graph[i].sum())
    # covert to tensor
    graph = torch.from_numpy(graph).float()
    return graph


# ElasticNet + RF : top 5 rel
def normed_adj_ERF_graph():
    gt = pd.read_csv('./data/ElaRF_ssm_11_relation.csv')
    gt = gt[['before', 'after']]
    KC = pd.read_csv('./data/kc_dedup_smath11.csv')
    kcs = KC['kc_uid'].unique().tolist()
    kcs.sort()

    graph = np.zeros([len(kcs), len(kcs)])
    # adj mat
    for i in range(len(kcs)):
        cond = gt.before == kcs[i]
        now_rels = gt[cond]['after'].tolist()[:5]
        for j in range(5):
            graph[kcs.index(kcs[i])][kcs.index(now_rels[j])] = 1
    # row normalization
    for i in range(len(graph)):
        if graph[i].sum() != 0:
            graph[i] = (graph[i]/graph[i].sum())
    # covert to tensor
    graph = torch.from_numpy(graph).float()
    return graph


# FIR-DKT
def normed_adj_FIR_graph():
    gt = pd.read_csv('./data/FIR_ssm11.csv')
    KC = pd.read_csv('./data/kc_dedup_smath11.csv')
    kcs = KC['kc_uid'].unique().tolist()
    kcs.sort()
    
    # find FIR best set of each KC
    best_sets = []
    for target in kcs:
        cond = gt.target== target
        t = gt[cond]['auc'].idxmax()
        best_sets.append(gt.iloc[t:t+1,].filter(regex='rel', axis=1).values.reshape(-1).tolist()[:5])
    graph = np.zeros([len(kcs), len(kcs)])
    # adj mat
    for i in range(len(kcs)):
        for j in range(5):
            graph[kcs.index(kcs[i])][kcs.index(best_sets[i][j])] = 1
    # row normalization
    for i in range(len(graph)):
        if graph[i].sum() != 0:
            graph[i] = (graph[i]/graph[i].sum())
    # covert to tensor
    graph = torch.from_numpy(graph).float()
    return graph


# 2-hop transition
def two_hop_transition_graph(question_list, seq_len_list, indices, student_num, concept_num):
    graph = np.zeros((concept_num, concept_num))
    student_dict = dict(zip(indices, np.arange(student_num)))
    for i in range(student_num):
        if i not in student_dict:
            continue
        questions = question_list[i]
        seq_len = seq_len_list[i]
        for j in range(seq_len - 1):
            pre = questions[j]
            next = questions[j + 1]
            graph[pre, next] += 1
    np.fill_diagonal(graph, 0)
    # norm
    for i in range(len(graph)):
        for j in range(len(graph)):
            if graph[i][j] != 0:
                graph[i][j] /= graph[i][j]
    
    # kc
    KC = pd.read_csv('./data/kc_dedup_smath11.csv')
    kcs = KC['kc_uid'].unique().tolist()
    kcs.sort()
    
    # 2-hop transition graph
    for i in range(len(kcs)-1, 0, -1):
        pres = list(np.where(np.array(graph).T[i] == 1))[0].tolist()
        for p in range(len(pres)):
            idx = pres[p]
            hops = list(np.where(np.array(graph).T[idx] == 1))[0].tolist()
            for h in range(len(hops)):
                if abs(int(kcs[hops[h]][-2:]) - int(kcs[i][-2:])) < 6:
                    graph[hops[h]][i] = 1
    
    # row normalization
    for i in range(len(graph)):
        if graph[i].sum() != 0:
            graph[i] = (graph[i]/graph[i].sum())

    # covert to tensor
    graph = torch.from_numpy(graph).float()
    return graph


# 2-hop transition
def two_hop_transition_daekyo_graph(question_list, seq_len_list, indices, student_num, concept_num):
    graph = np.zeros((concept_num, concept_num))
    student_dict = dict(zip(indices, np.arange(student_num)))
    for i in range(student_num):
        if i not in student_dict:
            continue
        questions = question_list[i]
        seq_len = seq_len_list[i]
        for j in range(seq_len - 1):
            pre = questions[j]
            next = questions[j + 1]
            graph[pre, next] += 1
    np.fill_diagonal(graph, 0)
    
    # kc
    KC = pd.read_csv('./data/kc_dedup_smath11.csv')
    kcs = KC['kc_uid'].unique().tolist()
    kcs.sort()
    
    # 2-hop transition graph
    for i in range(len(kcs)-1, 0, -1):
        pres = list(np.where(np.array(graph).T[i] == 1))[0].tolist()
        for p in range(len(pres)):
            idx = pres[p]
            hops = list(np.where(np.array(graph).T[idx] == 1))[0].tolist()
            for h in range(len(hops)):
                if abs(int(kcs[hops[h]][-2:]) - int(kcs[i][-2:])) < 6:
                    graph[hops[h]][i] = 1

    d_graph = normed_adj_graph()

    graph = np.array(graph) + np.array(d_graph)

    # 1??? ?????????
    for i in range(len(graph)):
        for j in range(len(graph)):
            if graph[i][j] != 0:
                graph[i][j] /= graph[i][j]

    # row normalization
    for i in range(len(graph)):
        if graph[i].sum() != 0:
            graph[i] = (graph[i]/graph[i].sum())

    # covert to tensor
    graph = torch.from_numpy(graph).float()
    return graph