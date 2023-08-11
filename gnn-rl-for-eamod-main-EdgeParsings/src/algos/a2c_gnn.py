"""
A2C-GNN
-------
This file contains the A2C-GNN specifications. In particular, we implement:
(1) GNNParser
    Converts raw environment observations to agent inputs (s_t).
(2) GNNActor:
    Policy parametrized by Graph Convolution Networks (Section III-C in the paper)
(3) GNNCritic:
    Critic parametrized by Graph Convolution Networks (Section III-C in the paper)
(4) A2C:
    Advantage Actor Critic algorithm using a GNN parametrization for both Actor and Critic.
"""

from operator import ne
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from torch.distributions import Dirichlet, Normal, LogNormal, Poisson
from torch_geometric.data import Data
from torch_geometric.nn import GCNConv, GATv2Conv
from torch_geometric.nn import global_mean_pool, global_max_pool

from torch_geometric.nn import MessagePassing
from torch.nn import Sequential as Seq, Linear, ReLU

from collections import namedtuple

SavedAction = namedtuple('SavedAction', ['log_prob', 'value'])
args = namedtuple('args', ('render', 'gamma', 'log_interval'))
args.render = True
args.gamma = 0.97
args.log_interval = 10

#########################################
############## PARSER ###################
#########################################


class GNNParser():
    """
    Parser converting raw environment observations to agent inputs (s_t).
    """

    def __init__(self, env, T=10, scale_factor=0.01, scale_price=0.1, input_size=20, MPNN=False):
        super().__init__()
        self.env = env
        self.T = T
        self.scale_factor = scale_factor
        self.price_scale_factor = scale_price
        self.input_size = 2 + (2 * T) # features in first two tensors = 2, features in last two tensors = 2 * T
        self.isMPNN = MPNN

    def parse_obs(self, version=0, charge_delta=0, max_charge=0, MPNN=False):
        # nodes

        # print(torch.tensor([float(n[1])/self.env.scenario.number_charge_levels for n in self.env.nodes]
        #                  ).view(1, 1, self.env.number_nodes).float().shape)
        # print(torch.tensor([self.env.acc[n][self.env.time+1]*self.scale_factor for n in self.env.nodes]
        #                  ).view(1, 1, self.env.number_nodes).float().shape)
        # print(torch.tensor([[(self.env.acc[n][self.env.time+1] + self.env.dacc[n][t])*self.scale_factor for n in self.env.nodes]
        #                   for t in range(self.env.time+1, self.env.time+self.T+1)]).view(1, self.T, self.env.number_nodes).float().shape)
        # print(torch.tensor([[sum([self.env.price[o[0], j][t]*self.scale_factor*self.price_scale_factor*(self.env.demand[o[0], j][t])*((o[1]-self.env.scenario.energy_distance[o[0], j]) >= int(not self.env.scenario.charging_stations[j]))
        #                   for j in self.env.region]) for o in self.env.nodes] for t in range(self.env.time+1, self.env.time+self.T+1)]).view(1, self.T, self.env.number_nodes).float().shape)        
        x = torch.cat((
            torch.tensor([float(n[1])/self.env.scenario.number_charge_levels for n in self.env.nodes]
                         ).view(1, 1, self.env.number_nodes).float(),
            torch.tensor([self.env.acc[n][self.env.time+1]*self.scale_factor for n in self.env.nodes]
                         ).view(1, 1, self.env.number_nodes).float(),
            torch.tensor([[(self.env.acc[n][self.env.time+1] + self.env.dacc[n][t])*self.scale_factor for n in self.env.nodes]
                          for t in range(self.env.time+1, self.env.time+self.T+1)]).view(1, self.T, self.env.number_nodes).float(),
            torch.tensor([[sum([self.env.price[o[0], j][t]*self.scale_factor*self.price_scale_factor*(self.env.demand[o[0], j][t])*((o[1]-self.env.scenario.energy_distance[o[0], j]) >= int(not self.env.scenario.charging_stations[j]))
                          for j in self.env.region]) for o in self.env.nodes] for t in range(self.env.time+1, self.env.time+self.T+1)]).view(1, self.T, self.env.number_nodes).float()),
                      dim=1).squeeze(0).view(self.input_size, self.env.number_nodes).T
        # edges 

        # versions for edge_index
        
        if version == 0:
        # V0 - all edges from AMoD passed into GCN
            edges = self.env.edges
            edge_index = self.env.gcn_edge_idx
            # print("# of EDGES PASSED TO GCN" + str(edge_index.shape[1]))
        
        if version == 1:
        # V1 - no edges, only self loops
            edges = []
            for o in self.env.nodes:
                for d in self.env.nodes:
                    if (o[0] == d[0] and o[1] == d[1]):
                        edges.append([o, d])
            edge_idx = torch.tensor([[], []], dtype=torch.long)
            for e in edges:
                origin_node_idx = self.env.nodes.index(e[0])
                destination_node_idx = self.env.nodes.index(e[1])
                new_edge = torch.tensor([[origin_node_idx], [destination_node_idx]], dtype=torch.long)
                edge_idx = torch.cat((edge_idx, new_edge), 1)
            edge_index = edge_idx
            # print("# of EDGES PASSED TO GCN" + str(edge_index.shape[1])) # = 12
        
        if version == 2:
        # V2 - combination of V0 and V1
            edges = []
            for o in self.env.nodes:
                for d in self.env.nodes:
                    if (o[0] == d[0] and o[1] == d[1]):
                        edges.append([o, d])
            edge_idx = torch.tensor([[], []], dtype=torch.long)
            for e in edges:
                origin_node_idx = self.env.nodes.index(e[0])
                destination_node_idx = self.env.nodes.index(e[1])
                new_edge = torch.tensor([[origin_node_idx], [destination_node_idx]], dtype=torch.long)
                edge_idx = torch.cat((edge_idx, new_edge), 1)
            edge_index = torch.cat((edge_idx, self.env.gcn_edge_idx), 1)
            # print("# of EDGES PASSED TO GCN" + str(edge_index.shape[1])) # = 32
        
        if version == 3:
        # V3 - grid style one-hop connections
            edges = []
            for o in self.env.nodes:
                for d in self.env.nodes:
                    if ((o[1] == d[1] and o[0] != d[0]) or ((o[1] == d[1] - 1) and (o[0] == d[0])) or ((o[1] == d[1] + 1) and (o[0] == d[0]))):
                        edges.append([o, d])
            edge_idx = torch.tensor([[], []], dtype=torch.long)
            for e in edges:
                origin_node_idx = self.env.nodes.index(e[0])
                destination_node_idx = self.env.nodes.index(e[1])
                new_edge = torch.tensor([[origin_node_idx], [destination_node_idx]], dtype=torch.long)
                edge_idx = torch.cat((edge_idx, new_edge), 1)
            edge_index = edge_idx
            # print("# of EDGES PASSED TO GCN" + str(edge_index.shape[1])) # = 32
        
        if version == 4:
        # V4 - combination of V3 and V1
            edges = []
            for o in self.env.nodes:
                for d in self.env.nodes:
                    if ((o[1] == d[1] and o[0] == d[0]) or (o[1] == d[1] and o[0] != d[0]) or ((o[1] == d[1] - 1) and (o[0] == d[0])) or ((o[1] == d[1] + 1) and (o[0] == d[0]))):
                        edges.append([o, d])
            edge_idx = torch.tensor([[], []], dtype=torch.long)
            for e in edges:
                origin_node_idx = self.env.nodes.index(e[0])
                destination_node_idx = self.env.nodes.index(e[1])
                new_edge = torch.tensor([[origin_node_idx], [destination_node_idx]], dtype=torch.long)
                edge_idx = torch.cat((edge_idx, new_edge), 1)
            edge_index = edge_idx
            # print("# of EDGES PASSED TO GCN" + str(edge_index.shape[1])) # = 44
        
        if version == 5:
        # V5 - all edges + artificial edges + "infeasible" charge edges + "unintuitive" road edges + self loops
            edges = []
            for o in self.env.nodes:
                for d in self.env.nodes:
                    # artificial edges
                    if ((o[0] != d[0]) and (o[1] + (charge_delta - 1) == d[1]) and (d[1] != max_charge)):
                        edges.append([o, d])
                    # "infeasible" charge edges
                    if ((o[0] == d[0]) and (o[1] + (charge_delta + 1) == d[1])):
                        edges.append([o, d])
                    # "unintuitive" road edges
                    if (o[0] == d[0] and (o[1] - 1 == d[1])):
                        edges.append([o, d])
                    # self loops
                    if (o[0] == d[0] and o[1] == d[1]):
                        edges.append([o, d])
            edge_idx = torch.tensor([[], []], dtype=torch.long)
            for e in edges:
                origin_node_idx = self.env.nodes.index(e[0])
                destination_node_idx = self.env.nodes.index(e[1])
                new_edge = torch.tensor([[origin_node_idx], [destination_node_idx]], dtype=torch.long)
                edge_idx = torch.cat((edge_idx, new_edge), 1)
            edge_idx = torch.cat((edge_idx, self.env.gcn_edge_idx), 1)
            edge_index = edge_idx
            # print("# of EDGES PASSED TO GCN" + str(edge_index.shape[1]))

        if version == 6:
        # V6 - all edges + artificial edges + "infeasible" charge edges + "unintuitive" road edges
            edges = []
            for o in self.env.nodes:
                for d in self.env.nodes:
                    # artificial edges
                    if ((o[0] != d[0]) and (o[1] + (charge_delta - 1) == d[1]) and (d[1] != max_charge)):
                        edges.append([o, d])
                    # "infeasible" charge edges
                    if ((o[0] == d[0]) and (o[1] + (charge_delta + 1) == d[1])):
                        edges.append([o, d])
                    # "unintuitive" road edges
                    if (o[0] == d[0] and (o[1] - 1 == d[1])):
                        edges.append([o, d])
            edge_idx = torch.tensor([[], []], dtype=torch.long)
            for e in edges:
                origin_node_idx = self.env.nodes.index(e[0])
                destination_node_idx = self.env.nodes.index(e[1])
                new_edge = torch.tensor([[origin_node_idx], [destination_node_idx]], dtype=torch.long)
                edge_idx = torch.cat((edge_idx, new_edge), 1)
            edge_idx = torch.cat((edge_idx, self.env.gcn_edge_idx), 1)
            edge_index = edge_idx
            # print("# of EDGES PASSED TO GCN" + str(edge_index.shape[1])) # = 36

        # default/global return (regular GCN)
        if not MPNN: 
            data = Data(x, edge_index)
            return data
        else:
        
        # edge features for MPNN implementation
        # potential edge features = 
        # number of vehicles travelling on given edge at a given time, price of rebalancing, demand
        
            edge_attr = []
            # edges.extend(self.env.edges) # needed when adding self-loops only
            for idx in range(edge_index.shape[1]):
                e = [self.env.nodes[edge_index[0, idx]], self.env.nodes[edge_index[1, idx]]]

                # reb_time, demand
                if e in self.env.edges:
                    i, j = self.env.edges[self.env.edges.index(e)]

                    demand_for_e_t = list(self.env.demand[i, j][t] for t in range(self.env.time+1, self.env.time+self.T+1))
                    price_for_e_t  = list(self.env.price[i, j][t] for t in range(self.env.time+1, self.env.time+self.T+1))
                    energy_distance_e_t = [self.env.scenario.energy_distance[i, j]] * self.T

                else:
                    demand_for_e_t = [0] * self.T
                    price_for_e_t = [0] * self.T
                    energy_distance_e_t = [0] * self.T
                
                while len(demand_for_e_t) < self.T or len(price_for_e_t) < self.T or len(energy_distance_e_t) < self.T:
                    demand_for_e_t.append(0)
                    price_for_e_t.append(0)
                    energy_distance_e_t.append(0)
                
                q = [demand_for_e_t + price_for_e_t + energy_distance_e_t]
                edge_attr.append(q)
        
            # Convert the list of edge attributes into a tensor
            tensor = torch.tensor(edge_attr)
            e = (tensor.view(1, np.prod(tensor.shape)).float()).squeeze(0).view(self.T * 3, edge_index.shape[1]).T

            # print("x shape: " + str(x.shape))
            # print("edge_index shape: " + str(edge_index.shape)) 
            # print("edge_attr shape: " + str(e.shape))
            data = Data(x, edge_index, edge_attr=e)
            
            return data

    def parse_obs_spatial(self):
        x = torch.cat((
            torch.tensor([self.env.acc_spatial[n][self.env.time+1]*self.scale_factor for n in self.env.nodes_spatial]).view(1, 1, self.env.number_nodes_spatial).float(), 
            torch.tensor([[(self.env.acc_spatial[n][self.env.time+1] + self.env.dacc_spatial[n][t])*self.scale_factor for n in self.env.nodes_spatial] \
                          for t in range(self.env.time+1, self.env.time+self.T+1)]).view(1, self.T, self.env.number_nodes_spatial).float(),
            torch.tensor([[sum([self.env.price[o,j][t]*self.scale_factor*self.price_scale_factor*(self.env.demand[o,j][t]) \
                          for j in self.env.region]) for o in self.env.region] for t in range(self.env.time+1, self.env.time+self.T+1)]).view(1, self.T, self.env.number_nodes_spatial).float()),
              dim=1).squeeze(0).view(self.input_size, self.env.number_nodes_spatial).T
        edge_index  = self.env.gcn_edge_idx_spatial
        data = Data(x, edge_index)
        return data

class EdgeConv(MessagePassing):
    def __init__(self, in_channels, out_channels):
        super().__init__(aggr='max') #  "Max" aggregation.

        self.mlp = Seq(Linear((in_channels), out_channels),
                        ReLU(),
                        Linear(out_channels, out_channels))

    def forward(self, x, edge_index, edge_attr):
        # x: node feature matrix of shape [num_nodes, in_channels]
        # edge_index: edge indices of shape [2, num_edges]
        # edge_attr has shape [E, in_channels]

        return self.propagate(edge_index, x=x, edge_attr=edge_attr) # shape = [num_nodes, out_channels]

    def message(self, x_i, x_j, edge_attr):
        # x_i has shape [E, in_channels] - source node features
        # x_j has shape [E, in_channels] - target node features
        # edge_attr has shape [E, in_channels]

        print("x_i shape: " + str(x_i.shape))
        print("x_j shape: " + str(x_j.shape))
        print("edge_attr shape: " + str(edge_attr.shape))

        tmp = torch.cat([x_i, x_j, edge_attr], dim=1)  
        # print("tmp shape: " + str(tmp.shape))

        return self.mlp(tmp)

#########################################
############## ACTOR ####################
#########################################


class GNNActor(nn.Module):
    """
    Actor \pi(a_t | s_t) parametrizing the concentration parameters of a Dirichlet Policy.
    """

    # MPNN implementation
    def __init__(self, in_channels, hidden_channels, T=10):
        super(GNNActor, self).__init__()

        self.conv1 = GCNConv(in_channels, hidden_channels)
        self.econv1 = EdgeConv(T * 3, hidden_channels)
        
        self.conv2 = GCNConv(hidden_channels * 2, hidden_channels)  # second convolution layer
        self.conv3 = GCNConv(hidden_channels, in_channels) # third convolution layer
        
        self.lin1 = nn.Linear(in_channels * 2, in_channels)
        self.lin2 = nn.Linear(in_channels, 128)
        self.lin3 = nn.Linear(128, 32)
        self.lin4 = nn.Linear(32, 2)

        # self.h_to_mu = nn.Linear(22 + hidden_dim, out_channels)
        # self.h_to_sigma = nn.Linear(22 + hidden_dim, out_channels)
        # self.h_to_concentration = nn.Linear(22 + hidden_dim, out_channels)

    def forward(self, data):
        data = data.to("cuda:0")

        out_1 = F.relu(self.conv1(data.x, data.edge_index))
        out_e1 = F.relu(self.econv1(data.x, data.edge_index, data.edge_attr))

        out_1c = torch.cat([out_1, out_e1], dim=1)
        
        out_2 = F.relu(self.conv2(out_1c, data.edge_index))
        out_3 = F.relu(self.conv3(out_2, data.edge_index))

        out_3c = torch.cat([data.x, out_3], dim=1)

        x = F.softplus(self.lin1(out_3c))
        x = F.softplus(self.lin2(x))
        x = F.softplus(self.lin3(x))
        x = self.lin4(x)
        return x[:, 0], x[:, 1]
        
        # mu, sigma = F.softplus(self.h_to_mu(x_pp)), F.softplus(self.h_to_sigma(x_pp))
        # alpha = F.softplus(self.h_to_concentration(x_pp))
        # return (mu, sigma), alpha

        # x = F.softplus(self.h_to_concentration(x_pp))
        # return x[:, 0], x[:, 1]

#########################################
############## CRITIC ###################
#########################################


class GNNCritic(nn.Module):
    """
    Critic parametrizing the value function estimator V(s_t).
    """

    # MPNN implementation
    def __init__(self, in_channels, hidden_channels, T=10):
        super(GNNCritic, self).__init__()

        self.conv1 = GCNConv(in_channels, hidden_channels)
        self.econv1 = EdgeConv(T * 3, hidden_channels)
        
        self.conv2 = GCNConv(hidden_channels * 2, hidden_channels)  # second convolution layer
        self.conv3 = GCNConv(hidden_channels, in_channels) # third convolution layer
        
        self.lin1 = nn.Linear(in_channels * 2, in_channels)
        self.lin2 = nn.Linear(in_channels, 128)
        self.lin3 = nn.Linear(128, 32)
        self.lin4 = nn.Linear(32, 1)

    def forward(self, data):
        data = data.to("cuda:0")

        out_1 = F.relu(self.conv1(data.x, data.edge_index))
        out_e1 = F.relu(self.econv1(data.x, data.edge_index, data.edge_attr))

        out_1c = torch.cat([out_1, out_e1], dim=1)
        
        out_2 = F.relu(self.conv2(out_1c, data.edge_index))
        out_3 = F.relu(self.conv3(out_2, data.edge_index))

        out_3c = torch.cat([data.x, out_3], dim=1)

        x = F.softplus(self.lin1(out_3c))
        x = F.softplus(self.lin2(x))
        x = F.softplus(self.lin3(x))
        x = self.lin4(x)
        return x

#########################################
############## A2C AGENT ################
#########################################


class A2C(nn.Module):
    """
    Advantage Actor Critic algorithm for the AMoD control problem. 
    """

    def __init__(self, env, eps=np.finfo(np.float32).eps.item(), device=torch.device("cuda:0"), T=10, lr_a=1.e-3, lr_c=1.e-3, grad_norm_clip_a=0.5, grad_norm_clip_c=0.5, seed=10, scale_factor=0.01, scale_price=0.1):
        super(A2C, self).__init__()
        self.env = env
        self.eps = eps
        self.T = T
        self.lr_a = lr_a
        self.lr_c = lr_c
        self.adapted_lr_a = lr_a
        self.adapted_lr_c = lr_c
        self.grad_norm_clip_a = grad_norm_clip_a
        self.grad_norm_clip_c = grad_norm_clip_c
        self.scale_factor = scale_factor
        self.scale_price = scale_price
        self.input_size = 2 + (2 * T) # features in first two tensors = 2, features in last two tensors = 2 * T

        torch.manual_seed(seed)
        self.device = device

        # MPNN implementation
        self.actor = GNNActor(in_channels=self.input_size, hidden_channels=self.input_size * 2, T=T)
        self.critic = GNNCritic(in_channels=self.input_size, hidden_channels=self.input_size * 2, T=T)
        self.obs_parser = GNNParser(self.env, T=T, input_size=self.input_size, scale_factor=scale_factor, scale_price=scale_price, MPNN=True)

        self.optimizers = self.configure_optimizers()

        # action & reward buffer
        self.saved_actions = []
        self.rewards = []
        self.means_concentration = []
        self.std_concentration = []
        self.to(self.device)

    def set_env(self, env):
        self.env = env
        self.obs_parser = GNNParser(self.env, T=self.T, input_size=self.input_size, scale_factor=self.scale_factor, scale_price=self.scale_price)
        self.means_concentration = []
        self.std_concentration = []

    def decay_learning_rate(self, scaler_a=1, scaler_c=1):
        self.adapted_lr_a *= scaler_a
        self.adapted_lr_c *= scaler_c
        self.optimizers = self.configure_optimizers()

    def forward(self, jitter=1e-20):
        """
        forward of both actor and critic
        """
        # parse raw environment data in model format
        x = self.parse_obs(version=0, charge_delta=self.env.scenario.charge_levels_per_charge_step, max_charge=self.env.scenario.number_charge_levels, MPNN=True).to(self.device)

        # MPNN implementation

        # parse raw environment data in model format
        # actor: computes concentration parameters of a X distribution
        a_out_concentration, a_out_is_zero = self.actor(x)
        concentration = F.softplus(a_out_concentration).reshape(-1) + jitter
        non_zero = torch.sigmoid(a_out_is_zero).reshape(-1)
        
        # critic: estimates V(s_t)
        value = self.critic(x)
        return concentration, non_zero, value

    def parse_obs(self, version, charge_delta, max_charge, MPNN):
        state = self.obs_parser.parse_obs(version, charge_delta, max_charge, MPNN)
        return state

    def select_action(self, eval_mode=False):
        concentration, non_zero, value = self.forward()
        
        concentration = concentration.to(self.device)
        non_zero = non_zero.to(self.device)
        value = value.to(self.device)
        
        # concentration, value = self.forward(obs)
        
        concentration_without_zeros = torch.tensor([], dtype=torch.float32)
        sampled_zero_bool_arr = []
        log_prob_for_zeros = 0
        for node in range(non_zero.shape[0]):
            sample = torch.bernoulli(non_zero[node])
            if sample > 0:
                indices = torch.tensor([node])
                new_element = torch.index_select(concentration, 0, indices)
                concentration_without_zeros = torch.cat((concentration_without_zeros, new_element), 0)
                sampled_zero_bool_arr.append(False)
                log_prob_for_zeros += torch.log(non_zero[node])
            else:
                sampled_zero_bool_arr.append(True)
                log_prob_for_zeros += torch.log(1-non_zero[node])
        if concentration_without_zeros.shape[0] != 0:
            mean_concentration = np.mean(concentration_without_zeros.detach().numpy())
            std_concentration = np.std(concentration_without_zeros.detach().numpy())
            self.means_concentration.append(mean_concentration)
            self.std_concentration.append(std_concentration)
            m = Dirichlet(concentration_without_zeros)
            if (eval_mode):
                dirichlet_action = concentration_without_zeros / (concentration_without_zeros.sum() + 1e-16)
            else:
                dirichlet_action = m.rsample()
            dirichlet_action_np = list(dirichlet_action.detach().numpy())
            log_prob_dirichlet = m.log_prob(dirichlet_action)
        else:
            log_prob_dirichlet = 0
        self.saved_actions.append(SavedAction(log_prob_dirichlet+log_prob_for_zeros, value))
        action_np = []
        dirichlet_idx = 0
        for node in range(non_zero.shape[0]):
            if sampled_zero_bool_arr[node]:
                action_np.append(0.)
            else:
                action_np.append(dirichlet_action_np[dirichlet_idx])
                dirichlet_idx += 1
        return action_np

    def select_equal_action(self):
        n_nodes = len(self.env.nodes)
        action = np.ones(n_nodes)/n_nodes
        return list(action)
    
    def select_action_MPNN(self, eval_mode=False):
        concentration, non_zero, value = self.forward()
        
        concentration = concentration.to(self.device)
        non_zero = non_zero.to(self.device)
        value = value.to(self.device)

        # concentration, value = self.forward(obs)
        concentration_without_zeros = torch.tensor([], dtype=torch.float32)
        sampled_zero_bool_arr = []
        log_prob_for_zeros = 0
        for node in range(non_zero.shape[0]):
            sample = torch.bernoulli(non_zero[node])
            if sample > 0:
                indices = torch.tensor([node])
                new_element = torch.index_select(concentration, 0, indices)
                concentration_without_zeros = torch.cat((concentration_without_zeros, new_element), 0)
                sampled_zero_bool_arr.append(False)
                log_prob_for_zeros += torch.log(non_zero[node])
            else:
                sampled_zero_bool_arr.append(True)
                log_prob_for_zeros += torch.log(1-non_zero[node])
        if concentration_without_zeros.shape[0] != 0:
            mean_concentration = np.mean(concentration_without_zeros.detach().numpy())
            std_concentration = np.std(concentration_without_zeros.detach().numpy())
            self.means_concentration.append(mean_concentration)
            self.std_concentration.append(std_concentration)
            m = Dirichlet(concentration_without_zeros)
            if (eval_mode):
                dirichlet_action = concentration_without_zeros / (concentration_without_zeros.sum() + 1e-16)
            else:
                dirichlet_action = m.rsample()
            dirichlet_action_np = list(dirichlet_action.detach().numpy())
            log_prob_dirichlet = m.log_prob(dirichlet_action)
        else:
            log_prob_dirichlet = 0
        self.saved_actions.append(SavedAction(log_prob_dirichlet+log_prob_for_zeros, value))
        action_np = []
        dirichlet_idx = 0
        for node in range(non_zero.shape[0]):
            if sampled_zero_bool_arr[node]:
                action_np.append(0.)
            else:
                action_np.append(dirichlet_action_np[dirichlet_idx])
                dirichlet_idx += 1
        
        return action_np
    
    def select_action_GAT(self, eval_mode=False):
        concentration, non_zero, value = self.forward()
        concentration = concentration.to(self.device)
        non_zero = non_zero.to(self.device)
        value = value.to(self.device)
        # concentration, value = self.forward(obs)
        concentration_without_zeros = torch.tensor([], dtype=torch.float32)
        sampled_zero_bool_arr = []
        log_prob_for_zeros = 0
        for node in range(non_zero.shape[0]):
            sample = torch.bernoulli(non_zero[node])
            if sample > 0:
                indices = torch.tensor([node])
                new_element = torch.index_select(concentration, 0, indices)
                concentration_without_zeros = torch.cat((concentration_without_zeros, new_element), 0)
                sampled_zero_bool_arr.append(False)
                log_prob_for_zeros += torch.log(non_zero[node])
            else:
                sampled_zero_bool_arr.append(True)
                log_prob_for_zeros += torch.log(1-non_zero[node])
        if concentration_without_zeros.shape[0] != 0:
            mean_concentration = np.mean(concentration_without_zeros.detach().numpy())
            std_concentration = np.std(concentration_without_zeros.detach().numpy())
            self.means_concentration.append(mean_concentration)
            self.std_concentration.append(std_concentration)
            m = Dirichlet(concentration_without_zeros)
            if (eval_mode):
                dirichlet_action = concentration_without_zeros / (concentration_without_zeros.sum() + 1e-16)
            else:
                dirichlet_action = m.rsample()
            dirichlet_action_np = list(dirichlet_action.detach().numpy())
            log_prob_dirichlet = m.log_prob(dirichlet_action)
        else:
            log_prob_dirichlet = 0
        self.saved_actions.append(SavedAction(log_prob_dirichlet+log_prob_for_zeros, value))
        action_np = []
        dirichlet_idx = 0
        for node in range(non_zero.shape[0]):
            if sampled_zero_bool_arr[node]:
                action_np.append(0.)
            else:
                action_np.append(dirichlet_action_np[dirichlet_idx])
                dirichlet_idx += 1
        return action_np

    def training_step(self):
        R = 0
        saved_actions = self.saved_actions
        policy_losses = []  # list to save actor (policy) loss
        value_losses = []  # list to save critic (value) loss
        returns = []  # list to save the true values

        # calculate the true value using rewards returned from the environment
        for r in self.rewards[::-1]:
            # calculate the discounted value
            R = r + args.gamma * R
            returns.insert(0, R)

        # returns = [r / 4390. for r in returns] # 49000 is the maximum reward
        returns = torch.tensor(returns)
        returns = (returns - returns.mean()) / (returns.std() + self.eps)

        log_probs = []
        values = []
        for (log_prob, value) in saved_actions:
            log_probs.append(log_prob.item())
            values.append(value.item())

        mean_value = np.mean(values)
        mean_concentration = np.mean(self.means_concentration)
        mean_std = np.mean(self.std_concentration)
        mean_log_prob = np.mean(log_probs)
        std_log_prob = np.std(log_probs)
        for (log_prob, value), R in zip(saved_actions, returns):
            # normed_log_prob = (log_prob - np.mean(log_probs)) / (np.std(log_probs) + self.eps)
            # normed_value = (value - mean_value) / (np.std(values) + self.eps)
            advantage = R - value.item()

            # calculate actor (policy) loss
            policy_losses.append(-log_prob * advantage)

            # calculate critic (value) loss using L1 smooth loss
            value_losses.append(F.smooth_l1_loss(value, torch.tensor([R]).to(self.device)))

        # take gradient steps
        self.optimizers['a_optimizer'].zero_grad()
        a_loss = torch.stack(policy_losses).sum()
        a_loss = torch.clamp(a_loss, -1000, 1000)
        # if np.abs(a_loss.item()) == 1000:
        #     self.decay_learning_rate(scaler_a=0.1)
        a_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.actor.parameters(), self.grad_norm_clip_a)
        self.optimizers['a_optimizer'].step()

        self.optimizers['c_optimizer'].zero_grad()
        v_loss = torch.stack(value_losses).sum()
        # v_loss = torch.clamp(v_loss, -1000, 1000)
        v_loss.backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), self.grad_norm_clip_c)
        self.optimizers['c_optimizer'].step()

        # reset rewards and action buffer
        del self.rewards[:]
        del self.saved_actions[:]
        return a_loss, v_loss, mean_value, mean_concentration, mean_std, mean_log_prob, std_log_prob

    def configure_optimizers(self):
        optimizers = dict()
        actor_params = list(self.actor.parameters())
        critic_params = list(self.critic.parameters())
        optimizers['a_optimizer'] = torch.optim.Adam(actor_params, lr=self.adapted_lr_a)
        # optimizers['a_optimizer'] = torch.optim.RAdam(actor_params, lr=self.adapted_lr_a)
        optimizers['c_optimizer'] = torch.optim.Adam(critic_params, lr=self.adapted_lr_c)
        # optimizers['c_optimizer'] = torch.optim.RAdam(critic_params, lr=self.adapted_lr_c)
        return optimizers

    def save_checkpoint(self, path='ckpt.pth'):
        checkpoint = dict()
        checkpoint['model'] = self.state_dict()
        for key, value in self.optimizers.items():
            checkpoint[key] = value.state_dict()
        torch.save(checkpoint, path)

    def load_checkpoint(self, path='ckpt.pth'):
        checkpoint = torch.load(path)
        self.load_state_dict(checkpoint['model'])
        for key, value in self.optimizers.items():
            self.optimizers[key].load_state_dict(checkpoint[key])

    def log(self, log_dict, path='log.pth'):
        torch.save(log_dict, path)