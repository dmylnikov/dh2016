"""
A thin wrapper for openAI gym environments that maintains a set of parallel games and has a method to generate interaction sessions
given agent one-step applier function
"""

import gym
from gym import wrappers
import numpy as np
from ...utils.layers import get_layer_dtype
from ...environment import SessionPoolEnvironment
from warnings import warn

function = type(lambda:0)

def GamePool(*args,**kwargs):
    raise ValueError("Deprecated. Use EnvPool(agent,env_title,n_parallel_agents) instead")

# A whole lot of space invaders
class EnvPool(object):
    def __init__(self, agent, make_env=lambda:gym.make("SpaceInvaders-v0"), n_games=1, max_size=None,
                 preprocess_observation = lambda obs:obs,agent_step=None):
        """
        A pool that stores several
           - game states (gym environment)
           - prev_observations - last agent observations
           - prev memory states - last agent hidden states

        and is capable of some auxilary actions like evaluating agent on one game session.

        :param make_env: a factory that produces environments OR a name of the gym environment.
                See gym.envs.registry.all()
        :param n_games: number of parallel games
        :param max_size: max pool size by default (if appending sessions)
        """
        if not isinstance(make_env, function):
            env_name = make_env
            make_env = lambda: gym.make(env_name)

        #create atari games
        self.make_env = make_env
        self.envs = [self.make_env() for _ in range(n_games)]
        self.preprocess_observation = preprocess_observation


        #initial observations
        self.prev_observations = [self.preprocess_observation(make_env.reset()) for make_env in self.envs]

        #agent memory variables (if you use recurrent networks
        self.prev_memory_states = [np.zeros((n_games,)+tuple(mem.output_shape[1:]),
                                   dtype=get_layer_dtype(mem))
                         for mem in agent.agent_states]

        #save agent
        self.agent = agent
        self.agent_step = agent_step or agent.get_react_function()

        # Create experience replay environment
        self.experience_replay = SessionPoolEnvironment(observations=agent.observation_layers,
                                                        actions=agent.action_layers,
                                                        agent_memories=agent.agent_states)
        self.max_size = max_size

        #whether particular session has just been terminated and needs restarting
        self.just_ended = [False] * len(self.envs)


    def interact(self, n_steps=100, verbose=False, add_last_observation=True):
        """generate interaction sessions with ataries (openAI gym atari environments)
        Sessions will have length n_steps.
        Each time one of games is finished, it is immediately getting reset


        :param n_steps: length of an interaction
        :param verbose: if True, prints small debug message whenever a game gets reloaded after end.
        :param add_last_observation: if True,appends the final state with
                state=final_state,action=-1,reward=0,new_memory_states=prev_memory_states, effectively making n_steps-1 records

        :returns: observation_log,action_log,reward_log,[memory_logs],is_alive_log,info_log
        :rtype: a bunch of tensors [batch, tick, size...],
                the only exception is info_log, which is a list of infos for [time][batch], None padded tick
        """


        def env_step(i,action):
            """environment reaction,
            :returns: observation, reward, is_alive, info"""

            if not self.just_ended[i]:
                new_observation, cur_reward,is_done,info = self.envs[i].step(action)
                if is_done:
                    # game ends now, will finalize on next tick
                    self.just_ended[i] = True
                new_observation = self.preprocess_observation(new_observation)

                #note: is_alive=True in any case because environment is still alive (last tick alive) in our notation
                return new_observation, cur_reward,True,info


            else:
                assert self.just_ended[i] == True

                # reset environment, get new observation to be used on next tick
                new_observation = self.preprocess_observation(self.envs[i].reset())

                #reset memory for new episode
                for m_i in range(len(new_memory_states)):
                    new_memory_states[m_i][i] = 0

                if verbose:
                    print("env %i reloaded" % i)

                self.just_ended[i] = False

                return new_observation,0,False,{'end':True}


        history_log = []

        for i in range(n_steps - int(add_last_observation)):
            res = self.agent_step(self.prev_observations, *self.prev_memory_states)
            actions, new_memory_states = res[0],res[1:]

            new_observations, cur_rewards, is_alive, infos = \
                zip(*map(env_step,range(len(self.envs)),actions))


            # append data tuple for this tick. Is alive is always True
            history_log.append((self.prev_observations, actions, cur_rewards, new_memory_states, is_alive, infos))

            self.prev_observations = new_observations
            self.prev_memory_states = new_memory_states

        if add_last_observation:
            fake_actions = np.array([env.action_space.sample() for env in self.envs])
            fake_rewards = np.zeros(shape=len(self.envs))
            is_fake_alive = np.ones(shape=len(self.envs))
            history_log.append((self.prev_observations,fake_actions,fake_rewards,self.prev_memory_states,
                                is_fake_alive,[None]*len(self.envs)))

        # cast to numpy arrays
        observation_log, action_log, reward_log, memories_log, is_alive_log, info_log = zip(*history_log)

        # tensor dimensions
        # [batch_i, time_i, observation_size...]
        observation_log = np.array(observation_log).swapaxes(0, 1)

        # [batch, time, units] for each memory tensor
        memories_log = map(lambda mem: np.array(mem).swapaxes(0, 1), zip(*memories_log))

        # [batch_i,time_i]
        action_log = np.array(action_log).swapaxes(0, 1)

        # [batch_i, time_i]
        reward_log = np.array(reward_log).swapaxes(0, 1)

        # [batch_i, time_i]
        is_alive_log = np.array(is_alive_log).swapaxes(0, 1).astype('uint8')


        return observation_log, action_log, reward_log, memories_log, is_alive_log, info_log


    def update(self,n_steps=100,append=False,max_size=None,add_last_observation=True,
               preprocess=lambda observations,actions,rewards,is_alive,h0:(observations,actions,rewards,is_alive,h0)):
        """ a function that creates new sessions and ads them into the pool
        :param n_steps: how many time steps in each session
        :param append: if True, appends sessions to the pool and crops at max_size
        :param max_size: if not None, substitutes default max_size (from __init__) for this update only
        :param add_last_observation: see interact param add_last_observation
        :param preprocess: a function that implements arbitrary processing of the sessions
                            takes AND outputs (observation_tensor, action_tensor,reward_tensor,is_alive_tensor,preceding_memory_states)
                            for param specs see .interact output format


        throwing the old ones away entirely for simplicity"""

        preceding_memory_states = list(self.prev_memory_states)

        # get interaction sessions
        observation_tensor, action_tensor, reward_tensor, _, is_alive_tensor, _ = self.interact(n_steps=n_steps,
                                                                                                add_last_observation=add_last_observation)

        observation_tensor, action_tensor, reward_tensor,is_alive_tensor,preceding_memory_states = \
            preprocess(observation_tensor, action_tensor,reward_tensor,is_alive_tensor,preceding_memory_states)

        # load them into experience replay environment
        if not append:
            self.experience_replay.load_sessions(observation_tensor, action_tensor, reward_tensor,
                                                 is_alive_tensor, preceding_memory_states)
        else:
            self.experience_replay.append_sessions(observation_tensor, action_tensor, reward_tensor,
                                                 is_alive_tensor, preceding_memory_states,
                                                   max_pool_size=max_size or self.max_size)


    def evaluate(self,n_games=1,save_path="./records", use_monitor=True,record_video=True,verbose=True,t_max=10000):
        """
        Plays an entire game start to end, records the logs(and possibly mp4 video), returns reward
        :param save_path: where to save the report
        :param record_video: if True, records mp4 video
        :return: total reward (scalar)
        """
        env = self.make_env()

        if not use_monitor and record_video:
            raise warn("Cannot video without gym monitor. If you still want video, set use_monitor to True")

        if record_video :
            #env.monitor.start(save_path, force=True)
            env = wrappers.Monitor(env, save_path, force=True)
        elif use_monitor:
            #env.monitor.start(save_path, lambda i: False, force=True)
            env = wrappers.Monitor(env, save_path, lambda i: False, force=True)

        game_rewards = []
        for _ in range(n_games):
            # initial observation
            observation = env.reset()
            # initial memory
            prev_memories = [np.zeros((1,) + tuple(mem.output_shape[1:]),
                                      dtype=get_layer_dtype(mem))
                             for mem in self.agent.agent_states]

            t = 0
            total_reward = 0
            while True:

                res = self.agent_step(self.preprocess_observation(observation)[None,...], *prev_memories)
                action, new_memories = res[0],res[1:]

                observation, reward, done, info = env.step(action[0])

                total_reward += reward
                prev_memories = new_memories

                if done or t >= t_max:
                    if verbose:
                        print("Episode finished after {} timesteps with reward={}".format(t + 1,total_reward))
                    break
                t += 1
            game_rewards.append(total_reward)

        env.close()
        del env
        return game_rewards
