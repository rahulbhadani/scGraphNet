import sys
import os
import numpy as np
import pickle as pkl
import networkx as nx
import scipy.sparse as sp
import scipy.io
from node2vec import Node2Vec
import torch
from torch import nn, optim
from torch.nn import functional as F
from torch.utils.data import Dataset, DataLoader
from benchmark_util import *
dir_path = os.path.dirname(os.path.realpath(__file__))

def parse_index_file(filename):
    index = []
    for line in open(filename):
        index.append(int(line.strip()))
    return index

#Original version of load_data
def load_data_ori(datasetName, discreteTag):
    # load the data: x, tx, allx, graph
    if discreteTag:
        names = ['xD', 'txD', 'allxD', 'graph']
    else:
        names = ['x', 'tx', 'allx', 'graph']
    objects = []
    for i in range(len(names)):
        with open(dir_path+"/data/sc/{}/ind.{}.{}".format(datasetName, datasetName, names[i]), 'rb') as f:
            if sys.version_info > (3, 0):
                objects.append(pkl.load(f, encoding='latin1'))
            else:
                objects.append(pkl.load(f))
    x, tx, allx, graph = tuple(objects)
    test_idx_reorder = parse_index_file(dir_path+"/data/sc/{}/ind.{}.test.index".format(datasetName, datasetName))
    test_idx_range = np.sort(test_idx_reorder)

    if datasetName == 'citeseer':
        # Fix citeseer datasetName (there are some isolated nodes in the graph)
        # Find isolated nodes, add them as zero-vecs into the right position
        test_idx_range_full = range(min(test_idx_reorder), max(test_idx_reorder)+1)
        tx_extended = sp.lil_matrix((len(test_idx_range_full), x.shape[1]))
        tx_extended[test_idx_range-min(test_idx_range), :] = tx
        tx = tx_extended

    features = sp.vstack((allx, tx)).tolil()
    features[test_idx_reorder, :] = features[test_idx_range, :]
    adj = nx.adjacency_matrix(nx.from_dict_of_lists(graph))

    return adj, features

def load_data(datasetName, discreteTag):
    # load the data: x, tx, allx, graph
    if discreteTag:
        names = ['xD', 'txD', 'allxD']
    else:
        names = ['x', 'tx', 'allx']
    objects = []
    for i in range(len(names)):
        with open(dir_path+"/data/sc/{}/ind.{}.{}".format(datasetName, datasetName, names[i]), 'rb') as f:
            if sys.version_info > (3, 0):
                objects.append(pkl.load(f, encoding='latin1'))
            else:
                objects.append(pkl.load(f))
    x, tx, allx = tuple(objects)
    test_idx_reorder = parse_index_file(dir_path+"/data/sc/{}/ind.{}.test.index".format(datasetName, datasetName))
    test_idx_range = np.sort(test_idx_reorder)

    if datasetName == 'citeseer':
        # Fix citeseer datasetName (there are some isolated nodes in the graph)
        # Find isolated nodes, add them as zero-vecs into the right position
        test_idx_range_full = range(min(test_idx_reorder), max(test_idx_reorder)+1)
        tx_extended = sp.lil_matrix((len(test_idx_range_full), x.shape[1]))
        tx_extended[test_idx_range-min(test_idx_range), :] = tx
        tx = tx_extended

    features = sp.vstack((allx, tx)).tolil()
    features[test_idx_reorder, :] = features[test_idx_range, :]

    return features

class scDatasetInter(Dataset):
    def __init__(self, features, transform=None):
        """
        Internal scData
        Args:
            construct dataset from features
        """
        self.features = features
        # Now lines are cells, and cols are genes
        # self.features = self.features.transpose()
        self.transform = transform        

    def __len__(self):
        return self.features.shape[0]
    
    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        sample = self.features[idx,:]

        if self.transform:
            sample = self.transform(sample)

        sample = torch.from_numpy(sample.toarray())
        return sample

class scDataset(Dataset):
    def __init__(self, datasetName=None, discreteTag=False, transform=None):
        """
        Args:
            datasetName (String): TGFb, etc.
            transform (callable, optional):
        """
        self.features = load_data(datasetName,discreteTag)
        # Now lines are cells, and cols are genes
        # self.features = self.features.transpose()
        # save nonzero
        self.nz_i,self.nz_j = self.features.nonzero()
        self.transform = transform        

    def __len__(self):
        return self.features.shape[0]
    
    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        sample = self.features[idx,:]

        if self.transform:
            sample = self.transform(sample)

        sample = torch.from_numpy(sample.toarray())
        return sample

class scDatasetDropout(Dataset):
    def __init__(self, datasetName=None, discreteTag=False, ratio=0.1, transform=None):
        """
        Args:
            datasetName (String): TGFb, etc.
            transform (callable, optional):
        """
        self.featuresOriginal = load_data(datasetName,discreteTag)
        self.ratio = ratio
        self.features, self.i, self.j, self.ix = impute_dropout(self.featuresOriginal, rate=self.ratio) 
        # Now lines are cells, and cols are genes
        # self.features = self.features.transpose()
        self.transform = transform        

    def __len__(self):
        return self.features.shape[0]
    
    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        sample = self.features[idx,:]

        if self.transform:
            sample = self.transform(sample)

        sample = torch.from_numpy(sample.toarray())
        return sample

# Original
def loss_function(recon_x, x, mu, logvar):
    '''
    Original: Classical loss function
    Reconstruction + KL divergence losses summed over all elements and batch
    '''
    # Original 
    BCE = F.binary_cross_entropy(recon_x, x.view(-1, 784), reduction='sum')

    # see Appendix B from VAE paper:
    # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
    # https://arxiv.org/abs/1312.6114
    # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())

    return BCE + KLD

# Graph
def loss_function_graph(recon_x, x, mu, logvar, adjsample=None, adjfeature=None, regulationMatrix=None, regularizer_type='noregu', modelusage='AE'):
    '''
    Regularized by the graph information
    Reconstruction + KL divergence losses summed over all elements and batch
    '''
    # Original 
    # BCE = F.binary_cross_entropy(recon_x, x.view(-1, 784), reduction='sum')
    # Graph
    target = x
    if regularizer_type == 'Graph' or regularizer_type == 'LTMG':
        target.requires_grad = True
    # Euclidean
    if regularizer_type == 'noregu':
        BCE = vallina_mse_loss_function(recon_x, target, reduction='sum')
    elif regularizer_type == 'Grap':
        BCE = graph_mse_loss_function(recon_x, target, adjsample, adjfeature, reduction='sum')
    elif regularizer_type == 'LTMG':
        BCE = regulation_mse_loss_function(recon_x, target, regulationMatrix, reduction='sum')
    
    # Entropy
    # BCE = graph_binary_cross_entropy(recon_x, target, adj, reduction='sum')
    # BCE = F.binary_cross_entropy(recon_x, target, reduction='sum')
    loss = BCE

    # see Appendix B from VAE paper:
    # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
    # https://arxiv.org/abs/1312.6114
    # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    if modelusage == 'VAE':
        KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
        loss = BCE + KLD    

    return loss

# change from pytorch
# Does not work now
# def graph_binary_cross_entropy(input, target, adj, weight=None, size_average=None,
#                          reduce=None, reduction='mean'):
#     # type: (Tensor, Tensor, Optional[Tensor], Optional[bool], Optional[bool], str) -> Tensor
#     r"""Function that measures the Binary Cross Entropy
#     between the target and the output.

#     See :class:`~torch.nn.BCELoss` for details.

#     Args:
#         input: Tensor of arbitrary shape
#         target: Tensor of the same shape as input
#         weight (Tensor, optional): a manual rescaling weight
#                 if provided it's repeated to match input tensor shape
#         size_average (bool, optional): Deprecated (see :attr:`reduction`). By default,
#             the losses are averaged over each loss element in the batch. Note that for
#             some losses, there multiple elements per sample. If the field :attr:`size_average`
#             is set to ``False``, the losses are instead summed for each minibatch. Ignored
#             when reduce is ``False``. Default: ``True``
#         reduce (bool, optional): Deprecated (see :attr:`reduction`). By default, the
#             losses are averaged or summed over observations for each minibatch depending
#             on :attr:`size_average`. When :attr:`reduce` is ``False``, returns a loss per
#             batch element instead and ignores :attr:`size_average`. Default: ``True``
#         reduction (string, optional): Specifies the reduction to apply to the output:
#             ``'none'`` | ``'mean'`` | ``'sum'``. ``'none'``: no reduction will be applied,
#             ``'mean'``: the sum of the output will be divided by the number of
#             elements in the output, ``'sum'``: the output will be summed. Note: :attr:`size_average`
#             and :attr:`reduce` are in the process of being deprecated, and in the meantime,
#             specifying either of those two args will override :attr:`reduction`. Default: ``'mean'``

#     Examples::

#         >>> input = torch.randn((3, 2), requires_grad=True)
#         >>> target = torch.rand((3, 2), requires_grad=False)
#         >>> loss = F.binary_cross_entropy(F.sigmoid(input), target)
#         >>> loss.backward()
#     """
#     if size_average is not None or reduce is not None:
#         reduction_enum = legacy_get_enum(size_average, reduce)
#     else:
#         reduction_enum = get_enum(reduction)
#     if target.size() != input.size():
#         print("Using a target size ({}) that is different to the input size ({}) is deprecated. "
#                       "Please ensure they have the same size.".format(target.size(), input.size()),
#                       stacklevel=2)
#     if input.numel() != target.numel():
#         raise ValueError("Target and input must have the same number of elements. target nelement ({}) "
#                          "!= input nelement ({})".format(target.numel(), input.numel()))

#     if weight is not None:
#         # new_size = _infer_size(target.size(), weight.size())
#         # weight = weight.expand(new_size)
#         print("Not implement yet from pytorch")

#     if args.regulized_type == 'Graph':
#         target.requires_grad = True
#         input = torch.matmul(input, adj)
#         target = torch.matmul(target, adj)

#     return torch._C._nn.binary_cross_entropy(
#         input, target, weight, reduction_enum)


# vallina mse
def vallina_mse_loss_function(input, target, size_average=None, reduce=None, reduction='mean'):
    # type: (Tensor, Tensor, Optional[bool], Optional[bool], str) -> Tensor
    r"""vallina_mse_loss_function(input, target, size_average=None, reduce=None, reduction='mean') -> Tensor

    Original: Measures the element-wise mean squared error.

    See :revised from pytorch class:`~torch.nn.MSELoss` for details.
    """
    if not (target.size() == input.size()):
        print("Using a target size ({}) that is different to the input size ({}). "
                      "This will likely lead to incorrect results due to broadcasting. "
                      "Please ensure they have the same size.".format(target.size(), input.size()))
    if size_average is not None or reduce is not None:
        reduction = legacy_get_string(size_average, reduce)
    # Now it use regulariz type to distinguish, it can be imporved later
    if target.requires_grad:
        ret = (input - target) ** 2
        if reduction != 'none':
            ret = torch.mean(ret) if reduction == 'mean' else torch.sum(ret)
    else:
        expanded_input, expanded_target = torch.broadcast_tensors(input, target)
        ret = torch._C._nn.mse_loss(expanded_input, expanded_target, get_enum(reduction))     
    return ret

# graphical mse
def graph_mse_loss_function(input, target, adjsample, adjfeature, size_average=None, reduce=None, reduction='mean'):
    # type: (Tensor, Tensor, Optional[bool], Optional[bool], str) -> Tensor
    r"""graph_mse_loss_function(input, target, adj, regularizer_type, size_average=None, reduce=None, reduction='mean') -> Tensor

    Measures the element-wise mean squared error in graph regularizor.

    See:revised from pytorch class:`~torch.nn.MSELoss` for details.
    """
    if not (target.size() == input.size()):
        print("Using a target size ({}) that is different to the input size ({}). "
                      "This will likely lead to incorrect results due to broadcasting. "
                      "Please ensure they have the same size.".format(target.size(), input.size()))
    if size_average is not None or reduce is not None:
        reduction = legacy_get_string(size_average, reduce)
    # Now it use regulariz type to distinguish, it can be imporved later
    ret = (input - target) ** 2
    if adjsample != None:
        ret = torch.matmul(adjsample, ret)
    if adjfeature != None:
        ret = torch.matmul(ret, adjfeature)
    if reduction != 'none':
        ret = torch.mean(ret) if reduction == 'mean' else torch.sum(ret)      
    return ret

# Regulation mse as the regularizor
# Now LTMG is set as the input
def regulation_mse_loss_function(input, target, regulationMatrix, reguPara=0.1, size_average=None, reduce=None, reduction='mean'):
    # type: (Tensor, Tensor, str, Optional[bool], Optional[bool], str) -> Tensor
    r"""regulation_mse_loss_function(input, target, regulationMatrix, regularizer_type, size_average=None, reduce=None, reduction='mean') -> Tensor

    Measures the element-wise mean squared error for regulation input, now only support LTMG.

    See :revised from pytorch class:`~torch.nn.MSELoss` for details.
    """
    if not (target.size() == input.size()):
        print("Using a target size ({}) that is different to the input size ({}). "
                      "This will likely lead to incorrect results due to broadcasting. "
                      "Please ensure they have the same size.".format(target.size(), input.size()))
    if size_average is not None or reduce is not None:
        reduction = legacy_get_string(size_average, reduce)
    # Now it use regulariz type to distinguish, it can be imporved later
    ret = (input - target) ** 2
    ret = torch.multiple(ret, reguPara * regulationMatrix)
    if reduction != 'none':
        ret = torch.mean(ret) if reduction == 'mean' else torch.sum(ret)      
    return ret

def legacy_get_enum(size_average, reduce, emit_warning=True):
    # type: (Optional[bool], Optional[bool], bool) -> int
    return get_enum(legacy_get_string(size_average, reduce, emit_warning))

# We use these functions in torch/legacy as well, in which case we'll silence the warning
def legacy_get_string(size_average, reduce, emit_warning=True):
    # type: (Optional[bool], Optional[bool], bool) -> str
    warning = "size_average and reduce args will be deprecated, please use reduction='{}' instead."

    if size_average is None:
        size_average = True
    if reduce is None:
        reduce = True

    if size_average and reduce:
        ret = 'mean'
    elif reduce:
        ret = 'sum'
    else:
        ret = 'none'
    if emit_warning:
        print(warning.format(ret))
    return ret

def get_enum(reduction):
    # type: (str) -> int
    if reduction == 'none':
        ret = 0
    elif reduction == 'mean':
        ret = 1
    elif reduction == 'elementwise_mean':
        print("reduction='elementwise_mean' is deprecated, please use reduction='mean' instead.")
        ret = 1
    elif reduction == 'sum':
        ret = 2
    else:
        ret = -1  # TODO: remove once JIT exceptions support control flow
        raise ValueError("{} is not a valid value for reduction".format(reduction))
    return ret

def generate_embedding(A, nodenum, dim=64):
    '''
    generate embedding from node2Vec
    Ref:
    https://github.com/eliorc/node2vec/blob/master/README.md
    return np.numArray(nodesize, dim)
    '''
    G = nx.from_scipy_sparse_matrix(A)
    # Precompute probabilities and generate walks - **ON WINDOWS ONLY WORKS WITH workers=1**
    node2vec = Node2Vec(G, dimensions=dim, walk_length=80, num_walks=10, workers=4)  # Use temp_folder for big graphs

    # Embed nodes
    model = node2vec.fit(window=10, min_count=1, batch_words=4)  # Any keywords acceptable by gensim.Word2Vec can be passed, `diemnsions` and `workers` are automatically passed (from the Node2Vec constructor)

    wv = model.wv
    embeddings = np.zeros([nodenum, dim], dtype='float32')
    sum_embeddings = 0
    empty_list = []
    for i in range(nodenum):
        if str(i) in wv:
            embeddings[i] = wv.word_vec(str(i))
            sum_embeddings += embeddings[i]
        else:
            empty_list.append(i)
    mean_embedding = sum_embeddings / (nodenum - len(empty_list))
    embeddings[empty_list] = mean_embedding
    return embeddings

def save_sparse_matrix(filename, x):
    x_coo = x.tocoo()
    row = x_coo.row
    col = x_coo.col
    data = x_coo.data
    shape = x_coo.shape
    np.savez(filename, row=row, col=col, data=data, shape=shape)

def load_sparse_matrix(filename):
    y = np.load(filename)
    z = scipy.sparse.coo_matrix((y['data'], (y['row'], y['col'])), shape=y['shape'])
    return z

def trimClustering(listResult,minMemberinCluster=5,maxClusterNumber=100):
    '''
    If the clustering numbers larger than certain number, use this function to trim. May have better solution
    '''
    numDict = {}
    for item in listResult:
        if not item in numDict:
            numDict[item] = 0
        else:
            numDict[item] = numDict[item]+1
    
    size = len(set(listResult))
    changeDict = {}
    for item in range(size):
        if numDict[item]<minMemberinCluster:
            changeDict[item] = ''
    
    count = 0
    for item in listResult:
        if item in changeDict:
            listResult[count] = maxClusterNumber
        count += 1

    return listResult

def readLTMG(datasetName):
    '''
    Read LTMG matrix
    '''
    matrix = pd.read_csv('biodata/scData/allBench/{}/T2000_UsingOriginalMatrix/T2000_LTMG.txt'.format(datasetName),header=None, delim_whitespace=True)
    matrix = matrix.to_numpy()
    matrix = matrix.transpose()
    return matrix
