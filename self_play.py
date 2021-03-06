import math
import time
import copy
import numpy
import ray
import torch

import models


@ray.remote
class SelfPlay:
    """
    Class which run in a dedicated thread to play games and save them to the replay-buffer.
    """

    def __init__(self, initial_weights, game, config, device):
        self.config = config
        self.game = game

        # Initialize the network
        self.model = models.MuZeroNetwork(
            self.config.observation_shape,
            len(self.config.action_space),
            self.config.encoding_size,
            self.config.hidden_size,
        )
        self.model.set_weights(initial_weights)
        self.model.to(torch.device(device))
        self.model.eval()

    def continuous_self_play(self, shared_storage, replay_buffer, test_mode=False):
        with torch.no_grad():
            while True:
                self.model.set_weights(
                    copy.deepcopy(ray.get(shared_storage.get_weights.remote()))
                )

                # Take the best action (no exploration) in test mode
                temperature = (
                    0
                    if test_mode
                    else self.config.visit_softmax_temperature_fn(
                        trained_steps=ray.get(shared_storage.get_infos.remote())[
                            "training_step"
                        ]
                    )
                )
                game_history = self.play_game(temperature, False)

                # Save to the shared storage
                if test_mode:
                    shared_storage.set_infos.remote(
                        "total_reward", sum(game_history.rewards)
                    )
                if not test_mode:
                    replay_buffer.save_game.remote(game_history)

                if not test_mode and self.config.self_play_delay:
                    time.sleep(self.config.self_play_delay)

    def play_game(self, temperature, render):
        """
        Play one game with actions based on the Monte Carlo tree search at each moves.
        """
        game_history = GameHistory(self.config.discount)
        observation = self.game.reset()
        game_history.observation_history.append(observation)
        done = False
        while not done and len(game_history.history) < self.config.max_moves:
            root = MCTS(self.config).run(self.model, observation, True)

            action = select_action(root, temperature)

            observation, reward, done = self.game.step(action)

            if render:
                self.game.render()
                print("Press enter to step")

            game_history.observation_history.append(observation)
            game_history.rewards.append(reward)
            game_history.history.append(action)
            game_history.store_search_statistics(root, self.config.action_space)

        self.game.close()
        return game_history


def select_action(node, temperature):
    """
    Select action according to the vivist count distribution and the temperature.
    The temperature is changed dynamically with the visit_softmax_temperature function 
    in the config.
    """
    visit_counts = numpy.array(
        [[child.visit_count, action] for action, child in node.children.items()]
    ).T
    if temperature == 0:
        action_pos = numpy.argmax(visit_counts[0])
    else:
        # See paper Data Generation appendix
        visit_count_distribution = visit_counts[0] ** (1 / temperature)
        visit_count_distribution = visit_count_distribution / sum(
            visit_count_distribution
        )
        action_pos = numpy.random.choice(
            len(visit_counts[1]), p=visit_count_distribution
        )

    if temperature == float("inf"):
        action_pos = numpy.random.choice(len(visit_counts[1]))

    return visit_counts[1][action_pos]


# Game independant
class MCTS:
    """
    Core Monte Carlo Tree Search algorithm.
    To decide on an action, we run N simulations, always starting at the root of
    the search tree and traversing the tree according to the UCB formula until we
    reach a leaf node.
    """

    def __init__(self, config):
        self.config = config

    def run(self, model, observation, add_exploration_noise):
        """
        At the root of the search tree we use the representation function to obtain a
        hidden state given the current observation.
        We then run a Monte Carlo Tree Search using only action sequences and the model
        learned by the network.
        """
        root = Node(0)
        observation = (
            torch.from_numpy(observation)
            .float()
            .unsqueeze(0)
            .to(next(model.parameters()).device)
        )
        _, expected_reward, policy_logits, hidden_state = model.initial_inference(
            observation
        )
        root.expand(
            self.config.action_space, expected_reward, policy_logits, hidden_state
        )
        if add_exploration_noise:
            root.add_exploration_noise(
                dirichlet_alpha=self.config.root_dirichlet_alpha,
                exploration_fraction=self.config.root_exploration_fraction,
            )

        min_max_stats = MinMaxStats()

        for _ in range(self.config.num_simulations):
            node = root
            search_path = [node]

            while node.expanded():
                action, node = self.select_child(node, min_max_stats)
                last_action = action
                search_path.append(node)

            # Inside the search tree we use the dynamics function to obtain the next hidden
            # state given an action and the previous hidden state
            parent = search_path[-2]
            value, reward, policy_logits, hidden_state = model.recurrent_inference(
                parent.hidden_state,
                torch.tensor([[last_action]]).to(parent.hidden_state.device),
            )
            node.expand(self.config.action_space, reward, policy_logits, hidden_state)

            self.backpropagate(search_path, value.item(), min_max_stats)

        return root

    def select_child(self, node, min_max_stats):
        """
        Select the child with the highest UCB score.
        """
        _, action, child = max(
            (self.ucb_score(node, child, min_max_stats), action, child)
            for action, child in node.children.items()
        )
        return action, child

    def ucb_score(self, parent, child, min_max_stats):
        """
        The score for a node is based on its value, plus an exploration bonus based on the prior.
        """
        pb_c = (
            math.log(
                (parent.visit_count + self.config.pb_c_base + 1) / self.config.pb_c_base
            )
            + self.config.pb_c_init
        )
        pb_c *= math.sqrt(parent.visit_count) / (child.visit_count + 1)

        prior_score = pb_c * child.prior
        value_score = min_max_stats.normalize(child.value())

        return prior_score + value_score

    def backpropagate(self, search_path, value, min_max_stats):
        """
        At the end of a simulation, we propagate the evaluation all the way up the tree
        to the root.
        """
        for node in search_path:
            # Always the same player, the other players minds should be modeled in network
            # because environment do not act always in the best way to make you lose
            node.value_sum += value  # if node.to_play == to_play else -value
            node.visit_count += 1
            min_max_stats.update(node.value())

            value = node.reward + self.config.discount * value


class Node:
    def __init__(self, prior):
        self.visit_count = 0
        self.to_play = -1
        self.prior = prior
        self.value_sum = 0
        self.children = {}
        self.hidden_state = None
        self.reward = 0

    def expanded(self):
        return len(self.children) > 0

    def value(self):
        if self.visit_count == 0:
            return 0
        return self.value_sum / self.visit_count

    def expand(self, actions, reward, policy_logits, hidden_state):
        """
        We expand a node using the value, reward and policy prediction obtained from the
        neural network.
        """
        self.reward = reward
        self.hidden_state = hidden_state
        policy = {a: math.exp(policy_logits[0][a]) for a in actions}
        policy_sum = sum(policy.values())
        for action, p in policy.items():
            self.children[action] = Node(p / policy_sum)

    def add_exploration_noise(self, dirichlet_alpha, exploration_fraction):
        """
        At the start of each search, we add dirichlet noise to the prior of the root to
        encourage the search to explore new actions.
        """
        actions = list(self.children.keys())
        noise = numpy.random.dirichlet([dirichlet_alpha] * len(actions))
        frac = exploration_fraction
        for a, n in zip(actions, noise):
            self.children[a].prior = self.children[a].prior * (1 - frac) + n * frac


class GameHistory:
    """
    Store only useful information of a self-play game.
    """

    def __init__(self, discount):
        self.observation_history = []
        self.history = []
        self.rewards = []
        self.child_visits = []
        self.root_values = []
        self.discount = discount

    def store_search_statistics(self, root, action_space):
        sum_visits = sum(child.visit_count for child in root.children.values())
        self.child_visits.append(
            [
                root.children[a].visit_count / sum_visits if a in root.children else 0
                for a in action_space
            ]
        )
        self.root_values.append(root.value())


class MinMaxStats:
    """
    A class that holds the min-max values of the tree.
    """

    def __init__(self):
        self.maximum = -float("inf")
        self.minimum = float("inf")

    def update(self, value):
        self.maximum = max(self.maximum, value)
        self.minimum = min(self.minimum, value)

    def normalize(self, value):
        if self.maximum > self.minimum:
            # We normalize only when we have set the maximum and minimum values
            return (value - self.minimum) / (self.maximum - self.minimum)
        return value
