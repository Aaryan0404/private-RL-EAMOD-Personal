from __future__ import print_function
import argparse
import os
import gurobipy as gp
from tqdm import trange
import numpy as np
import torch
import json
import os
import wandb

from src.envs.amod_env import Scenario, AMoD
from src.algos.a2c_gnn import A2C
from src.algos.reb_flows_solver import RebalFlowSolver
from src.algos.pax_flows_solver import PaxFlowsSolver
from src.misc.utils import dictsum

def create_scenario(json_file_path, energy_file_path, seed=10):
    f = open(json_file_path)
    energy_dist = np.load(energy_file_path)
    data = json.load(f)
    tripAttr = data['demand']
    reb_time = data['rebTime']
    total_acc = data['totalAcc']
    spatial_nodes = data['spatialNodes']
    tf = data['episodeLength']
    number_charge_levels = data['chargelevels']
    # charge_time_per_level = data['chargeTime']
    chargers = data['chargeLocations']
    cars_per_station_capacity = data['carsPerStationCapacity']

    scenario = Scenario(spatial_nodes=spatial_nodes, charging_stations=chargers, cars_per_station_capacity=cars_per_station_capacity,  number_charge_levels=number_charge_levels, 
                        energy_distance=energy_dist, tf=tf, sd=seed, tripAttr = tripAttr, demand_ratio=1, reb_time=reb_time, total_acc = total_acc)
    return scenario

parser = argparse.ArgumentParser(description='A2C-GNN')

# Simulator parameters
parser.add_argument('--seed', type=int, default=10, metavar='S',
                    help='random seed (default: 10)')
parser.add_argument('--demand_ratio', type=float, default=0.5, metavar='S',
                    help='demand_ratio (default: 0.5)')
parser.add_argument('--beta', type=int, default=0.5, metavar='S',
                    help='cost of rebalancing (default: 0.5)')

# Model parameters
parser.add_argument('--baseline_type', type=str, default=None,
                    help='defines the mode for agent evaluation')
parser.add_argument('--directory', type=str, default='saved_files',
                    help='defines directory where to save files')

args = parser.parse_args()
args.cuda = torch.cuda.is_available()
device = torch.device("cuda" if args.cuda else "cpu")

problem_folder = 'Toy'
file_path = os.path.join('data', problem_folder, 'scenario_train_3_2.json')
experiment = args.baseline_type + '_baseline_testing_' + file_path
energy_dist_path = os.path.join('data', problem_folder, 'energy_distance_3x2.npy')
test_scenario = create_scenario(file_path, energy_dist_path)
# env = AMoD(test_scenario, beta=args.beta)
env = AMoD(test_scenario)
# Initialize A2C-GNN
model = A2C(env=env).to(device)
tf = env.tf

# set up wandb
wandb.init(
      # Set the project where this run will be logged
      project='e-amod', 
      # pass a run name 
      name=experiment, 
      # Track hyperparameters and run metadata
      config={
        "number_chargelevels": env.scenario.number_charge_levels,
        "number_spatial_nodes": env.scenario.spatial_nodes,
        "dataset": file_path,
        "number_vehicles_per_node_init": env.G.nodes[(0,1)]['accInit'],
        "charging_stations": list(env.scenario.charging_stations),
      })

# # set Gurobi environment
# gurobi_env = gp.Env(empty=True)
# gurobi_env.setParam('WLSACCESSID', '8cad5801-28d8-4e2e-909e-3a7144c12eb5')
# gurobi_env.setParam('WLSSECRET', 'a25b880b-8262-492f-a2e5-e36d6d78cc98')
# gurobi_env.setParam('LICENSEID', 799876)
# gurobi_env.setParam("OutputFlag",0)
# gurobi_env.start()

# set Gurobi environment Karthik
gurobi_env = gp.Env(empty=True)
gurobi = "Karthik"
gurobi_env.setParam('WLSACCESSID', 'ad632625-ffd3-460a-92a0-6fef5415c40d')
gurobi_env.setParam('WLSSECRET', '60bd07d8-4295-4206-96e2-bb0a99b01c2f')
gurobi_env.setParam('LICENSEID', 849913)
gurobi_env.setParam("OutputFlag",0)
gurobi_env.start()

T = tf #set episode length
#Initialize lists for logging
log = {'test_reward': [], 
        'test_served_demand': [], 
        'test_reb_cost': []}
episode_reward = 0
episode_served_demand = 0
episode_rebalancing_cost = 0
# obs = env.reset()
model.set_env(env)
done = False
step = 0

pax_flows_solver = None
while(not done):
    # take matching step (Step 1 in paper)
    if step == 0:
        # initialize optimization problem in the first step
        pax_flows_solver = PaxFlowsSolver(env=env,gurobi_env=gurobi_env)
        step = 1
    else:
        pax_flows_solver.update_constraints()
        pax_flows_solver.update_objective()
    _, paxreward, done, info_pax = env.pax_step(pax_flows_solver=pax_flows_solver)
    episode_reward += paxreward

    # use GNN-RL policy (Step 2 in paper)
    action_rl = model.select_equal_action()
    # transform sample from Dirichlet into actual vehicle counts (i.e. (x1*x2*..*xn)*num_vehicles)
    total_acc = sum(env.acc[n][env.time+1] for n in env.nodes)
    desiredAcc = {env.nodes[i]: int(action_rl[i] * dictsum(env.acc,env.time+1)) for i in range(len(env.nodes))}
    total_desiredAcc = sum(desiredAcc[n] for n in env.nodes)
    missing_cars = total_acc - total_desiredAcc
    most_likely_node = np.argmax(action_rl)
    if missing_cars != 0:
        desiredAcc[env.nodes[most_likely_node]] += missing_cars   
        total_desiredAcc = sum(desiredAcc[n] for n in env.nodes)
    assert total_desiredAcc == total_acc
    for n in env.nodes:
        assert desiredAcc[n] >= 0
    # solve minimum rebalancing distance problem (Step 3 in paper)
    rebAction = RebalFlowSolver(env=env, desiredAcc=desiredAcc, gurobi_env=gurobi_env)
    # Take action in environment
    new_obs, rebreward, done, info = env.reb_step(rebAction)
    episode_reward += rebreward
    # track performance over episode
    episode_served_demand += info['served_demand']
    episode_rebalancing_cost += info['rebalancing_cost']
# Send current statistics to wandb
wandb.log({"Episode": 0, "Reward": episode_reward, "ServedDemand": episode_served_demand, "Reb. Cost": episode_rebalancing_cost})
# wandb.summary['max_reward'] = best_rewards.max()
wandb.summary['max_reward'] = episode_reward
wandb.finish()
print("done")
    