import torch


class FullyConnectedNetwork(torch.nn.Module):
    def __init__(
        self, input_size, layers_sizes, output_size, activation=torch.nn.Tanh(), conv_layers=0
    ):
        super(FullyConnectedNetwork, self).__init__()
        layers_sizes.insert(0, input_size)
        layers = []
        if len(layers_sizes) > 1:
            for i in range(len(layers_sizes) - 1):
                layers.extend(
                    [
                        torch.nn.Linear(layers_sizes[i], layers_sizes[i + 1]),
                        torch.nn.ReLU(),
                    ]
                )
        layers.append(torch.nn.Linear(layers_sizes[-1], output_size))
        if activation:
            layers.append(activation)
        self.layers = torch.nn.ModuleList(layers)

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return x

class SafelifeConvNetork(torch.nn.Module):
    "This is hardcoded due to artistic disagreements with this codebase's layout :)"
    def __init__(self):
        layer1 = torch.nn.Conv2D

    def forward(self, x):


# TODO: unified residual network
class MuZeroNetwork(torch.nn.Module):
    def __init__(self, observation_size, action_space_size, encoding_size, hidden_size):
        super().__init__()
        self.action_space_size = action_space_size

        self.representation_network = FullyConnectedNetwork(
            observation_size, [], encoding_size
        )

        self.dynamics_encoded_state_network = FullyConnectedNetwork(
            encoding_size + self.action_space_size, [hidden_size], encoding_size
        )
        self.dynamics_reward_network = FullyConnectedNetwork(
            encoding_size + self.action_space_size, [hidden_size], 1
        )

        self.prediction_policy_network = FullyConnectedNetwork(
            encoding_size, [], self.action_space_size, activation=None
        )
        self.prediction_value_network = FullyConnectedNetwork(
            encoding_size, [], 1, activation=None
        )

    def prediction(self, encoded_state):
        policy_logit = self.prediction_policy_network(encoded_state)
        value = self.prediction_value_network(encoded_state)
        return policy_logit, value

    def representation(self, observation):
        return self.representation_network(observation)

    def dynamics(self, encoded_state, action):
        action_one_hot = (
            torch.zeros((action.shape[0], self.action_space_size))
            .to(action.device)
            .float()
        )
        action_one_hot.scatter_(1, action.long(), 1.0)

        x = torch.cat((encoded_state, action_one_hot), dim=1)
        next_encoded_state = self.dynamics_encoded_state_network(x)
        reward = self.dynamics_reward_network(x)
        return next_encoded_state, reward

    def initial_inference(self, observation):
        encoded_state = self.representation(observation)
        policy_logit, value = self.prediction(encoded_state)
        return (
            value,
            torch.zeros(len(observation)).to(observation.device),
            policy_logit,
            encoded_state,
        )

    def recurrent_inference(self, encoded_state, action):
        next_encoded_state, reward = self.dynamics(encoded_state, action)
        policy_logit, value = self.prediction(next_encoded_state)
        return value, reward, policy_logit, next_encoded_state

    def get_weights(self):
        return {key: value.cpu() for key, value in self.state_dict().items()}

    def set_weights(self, weights):
        self.load_state_dict(weights)
