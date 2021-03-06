import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from torch_geometric.nn import MessagePassing
from torch_geometric.utils import add_self_loops, softmax
from torch_geometric.data import Data, DataLoader

def get_embeddings(cap, data, distMat, routes):
    result = []
    norm = data[0,4]-data[0,3]
    result.append(data[:,0]/norm) # x normalized by depot's 时间窗跨度
    result.append(data[:,1]/norm) # y normalized by depot's 时间窗跨度
    result.append(data[:,2]/cap)
    result.append(data[:,3]/norm) # start_time normalized by depot's 时间窗跨度
    result.append(data[:,4]/norm) # end_time normalized by depot's 时间窗跨度
    result.append(data[:,5]/norm) # service_time normalized by depot's 时间窗跨度
    result.append((data[:,4]-data[:,3])/norm) # 时间窗跨度除以 depot's 时间窗跨度
    result.append(np.zeros(len(data))) # cumulative demand normalized by cap
    result.append(np.zeros(len(data))) # cumulative distance normalized by depot's 时间窗跨度
    result.append(np.zeros(len(data))) # total demand normalized by cap
    result.append(np.zeros(len(data))) # total distance normalized by depot's 时间窗跨度
    for r in routes:
        complete_r = [0]+r+[0]
        cum_demand = 0
        cum_dist = 0
        for i in range(1, len(complete_r)-1):
            cum_demand+=data[complete_r[i],2]
            cum_dist+=distMat[complete_r[i-1],complete_r[i]]
            result[7][complete_r[i]] = cum_demand
            result[8][complete_r[i]] = cum_dist
        for i in range(1, len(complete_r)-1):
            result[9][complete_r[i]] = cum_demand
            result[10][complete_r[i]] = cum_dist

    result[7]/=cap
    result[8]/=norm
    result[9]/=cap
    result[10]/=norm
    return np.transpose(np.array(result))

class Net_Generic(nn.Module):
    
    def __init__(self, lr, input_dim, hidden, output_dim, device="cpu:0"):
        super(Net_Generic, self).__init__()
        self.lr = lr
        self.fcs = []
        self.l1 = nn.Linear(input_dim, hidden)
        self.l2 = nn.Linear(hidden, hidden)
        self.l3 = nn.Linear(hidden, output_dim)
        
        self.optimizer = optim.Adam(self.parameters(), lr=self.lr)
        self.device = torch.device(device)
        self.to(self.device)
    
    def forward(self, state):
        # state is a torch.Tensor
        x = state.to(self.device)
        x = F.relu(self.l1(x))
        x = F.relu(self.l2(x))
        x = self.l3(x)
        return x

class GATConv(MessagePassing):
    
    def __init__(self, in_channels, out_channels, negative_slope=0.2, device="cpu:0"):
        super(GATConv, self).__init__(aggr='add')
        self.negative_slope = negative_slope
        self.lin = torch.nn.Linear(in_channels, out_channels)
        self.attn = nn.Linear(2 * out_channels, out_channels)
        self.device = torch.device(device)
        self.to(self.device)

    def forward(self, x, edge_index):
        edge_index, _ = add_self_loops(edge_index, num_nodes=x.size(0))
        x = self.lin(x)
        return self.propagate(edge_index, size=(x.size(0), x.size(0)), x=x)

    def message(self, x_i, x_j, edge_index_i, size_i):
        # x_j -> x_i
        x = torch.cat([x_i, x_j], dim=-1)
        alpha = self.attn(x)
        alpha = F.leaky_relu(alpha, self.negative_slope)
        alpha = softmax(alpha, edge_index_i, size_i)
        return x_j * alpha

    def update(self, aggr_out):
        return aggr_out

class Attention(nn.Module):
    def __init__(self, hidden_size):
        super(Attention, self).__init__()
        self.hidden_size = hidden_size
        self.W1 = nn.Linear(hidden_size, hidden_size, bias=False)
        self.W2 = nn.Linear(hidden_size, hidden_size, bias=False)
        self.vt = nn.Linear(hidden_size, 1, bias=False)

    def forward(self, x1, x2):
        wx1 = self.W1(x1)
        wx2 = self.W2(x2)
        u_i = self.vt(torch.tanh(wx1 + wx2)).squeeze(-1)
        return u_i

class Net_Actor(nn.Module):

    def __init__(self, lr, embedding_dim, node_dim, n_nodes, device="cpu:0"):
        super(Net_Actor, self).__init__()
        self.lr = lr
        self.embedding_dim = embedding_dim
        self.node_dim = node_dim
        self.n_nodes = n_nodes
        
        self.gat_0 = GATConv(embedding_dim, node_dim, device=device)
        self.gat_1 = GATConv(node_dim, node_dim, device=device)
        self.gat_2 = GATConv(node_dim, node_dim, device=device)
        self.lin_prob = torch.nn.Linear(node_dim, 1)
        
        self.attn = Attention(node_dim)
        
        self.optimizer = optim.Adam(self.parameters(), lr=self.lr)
        self.device = torch.device(device)
        self.to(self.device)

    def forward(self, data):
        x, e_r0, e_r1, e_n = data.x, data.edge_index_r0, data.edge_index_r1, data.edge_index_n
        gat_0_out = self.gat_0(x, e_n)
        gat_1_out = self.gat_1(gat_0_out, e_r1)
        gat_2_out = self.gat_2(gat_1_out, e_r0)
        gat_3_out = self.gat_1(gat_2_out, e_r1)
        gat_4_out = self.gat_2(gat_3_out, e_r0)
        x = gat_4_out.reshape((-1, self.n_nodes, self.node_dim))
        h = torch.mean(x, dim=1)
        
        prob1 = self.lin_prob(x)
        prob1 = prob1.reshape((-1, self.n_nodes))
        prob1 = F.softmax(prob1[:,1:], dim=-1)
        m1 = torch.distributions.categorical.Categorical(prob1)
        index1 = 1+m1.sample()
        
        sampled_x = x[torch.arange(index1.size(0)), index1].unsqueeze(1)
        prob2 = self.attn(sampled_x, x)
        prob2[torch.arange(index1.size(0)), index1] = float("-inf")
        prob2 = F.softmax(prob2[:,1:], dim=-1)
        m2 = torch.distributions.categorical.Categorical(prob2)
        index2 = 1+m2.sample()
        
        probs = prob1[torch.arange(index1.size(0)), index1-1]+prob2[torch.arange(index2.size(0)), index2-1]
        log_probs = torch.log(probs)
        
        return h, index1, index2, log_probs
    
class Agent(object):
    
    def __init__(self,
                 lr_actor,
                 lr_critic,
                 embedding_dim,
                 node_dim,
                 n_nodes,
                 critic_hidden,
                 gamma=0.99,
                 device="cpu:0"):
        
        self.gamma  = gamma
        self.actor = Net_Actor(lr_actor, embedding_dim, node_dim, n_nodes, device=device)
        self.critic = Net_Generic(lr_critic, node_dim, critic_hidden, 1, device=device)
    
    def choose_action(self, data):
        h, index1, index2, log_probs = self.actor(data)
        return h, index1, index2, log_probs
    
    def learn(self, h, reward, h_, log_prob):
        self.actor.optimizer.zero_grad()
        self.critic.optimizer.zero_grad()
        
        critic_value = torch.reshape(self.critic(h), (-1,))
        critic_value_ = torch.reshape(self.critic(h_), (-1,))
        
        td = self.gamma * critic_value_ + reward - critic_value
        actor_loss = td * log_prob
        critic_loss = td**2
        loss = torch.mean(critic_loss) - torch.mean(actor_loss)
        
        loss.backward(retain_graph=True)
        self.actor.optimizer.step()
        self.critic.optimizer.step()
    
    def save(self, actor_path, critic_path):
        torch.save(self.actor.state_dict(), actor_path)
        torch.save(self.critic.state_dict(), critic_path)
    
    def load(self, actor_path, critic_path):
        self.actor.load_state_dict(torch.load(actor_path))
        self.critic.load_state_dict(torch.load(critic_path))