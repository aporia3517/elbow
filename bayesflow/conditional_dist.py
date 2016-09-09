import numpy as np
import tensorflow as tf

import uuid

from util.misc import concrete_shape


class ConditionalDistribution(object):
    """
    
    Generic object representing a conditional distribution: a single output conditioned on some set of inputs. 
    Unconditional distributions are the special case where the inputs are the empty set. 

    The inputs are assumed to be random variables described by their own respective distributions. 
    Thus the object graph implicitly represents a directed graphical model (Bayesian network).
    """

    def __init__(self, shape=None, minibatch_scale_factor = None,
                 name=None, model="auto", **kwargs):

        if name is None:
            name = str(uuid.uuid4().hex)[:6]
            print "constructed name", name
        self.name = name

        self._q_distribution = None
        self.minibatch_scale_factor = minibatch_scale_factor

        self.shape = shape
        
        # store map of input local names to the canonical names, that
        # is, (node, name) pairs -- modeling those params.
        self.inputs_random = {}
        self.inputs_nonrandom = {}
        self.input_shapes = self._setup_inputs(result_shape=shape, **kwargs)
    
        # if not already specified, compute the shape of the output at
        # this node as a function of its inputs
        if shape is None:
            input_shapes = {input_name+"_shape": shape for (input_name, shape) in self.input_shapes.items()}
            self.shape = self._compute_shape(**input_shapes)
            
        # TODO do something sane here...
        self.dtype = tf.float32

        input_samples = {}
        for param, node in self.inputs_random.items():
            input_samples[param] = node._sampled
        input_samples.update(self.inputs_nonrandom)
        self._sampled, self._sampled_entropy = self._sample_and_entropy(**input_samples)

        self.__dict__.update(input_samples)
        self.__dict__.update(self.derived_parameters(**input_samples))
        
    def _setup_inputs(self, result_shape=None, **kwargs):
        """
        Called by the constructor to process inputs passed as random variables, 
        fixed values, or (if passed as None) parameters to be optimized over. 

        The result is to populate 
          self.inputs_random
          self.inputs_nonrandom
        which map input names to ConditionalDist objects and Tensors, respectively. 
        We also return a dictionary mapping inputs to their shapes. 

        This method can be overridden by classes that need to do some other
        form of crazy input processing. 
        """

        input_shapes = {}
        free_inputs = []
        
        for input_name, default_constructor in self.inputs().items():
            # inputs can be provided as constants, or nodes modeled by bayesflow distributions.
            if isinstance(kwargs[input_name], ConditionalDistribution):
                node = kwargs[input_name]
                self.inputs_random[input_name] = node
                input_shapes[input_name] = node.shape
            elif kwargs[input_name] is not None:
                # if inputs are provided as TF or numpy values, just store that directly
                v = kwargs[input_name]
                if isinstance(v, np.ndarray) and v.dtype in (np.int, np.int32, np.int64):
                    tf_value = tf.convert_to_tensor(v, dtype=tf.int32)
                else:
                    tf_value = tf.convert_to_tensor(v, dtype=tf.float32)
                self.inputs_nonrandom[input_name] = tf_value
                input_shapes[input_name] = concrete_shape(tf_value.get_shape())
            elif kwargs[input_name] is None:
                free_inputs.append((input_name, default_constructor))

        # free inputs will be optimized over
        for free_input, constructor in free_inputs:
            shape = self._input_shape(free_input, result=result_shape, **input_shapes)
            input_shapes[free_input] = shape
            self.inputs_nonrandom[free_input] = constructor(shape=shape)

        return input_shapes
            
    def _sample_and_entropy(self, **kwargs):
        """
        Models with monte carlo entropy define the (stochastic) entropy in terms 
        of the log probability of a sample, so for those models it's useful
        to jointly generate a sample and a stochastic estimate of the entropy.
        By default, however, we just call the methods separately. 
        """
        sample = self._sample(**kwargs)
        self._sampled=sample
        entropy = self._entropy(**kwargs)
        return sample, entropy
    
    def sample(self, seed=0):
        init = tf.initialize_all_variables()
        tf.set_random_seed(seed)

        sess = tf.Session()
        sess.run(init)
        return sess.run(self._sampled)
        
    def _parameterized_logp(self, *args, **kwargs):
        """
        Compute the log probability using the values of all fixed input
        parameters associated with this graph node.
        """
        kwargs.update(self.inputs_nonrandom)
        return self._logp(*args, **kwargs)

    def _parameterized_sample(self, *args, **kwargs):
        kwargs.update(self.inputs_nonrandom)
        return self._sample(*args, **kwargs)

    def _entropy(self, *args, **kwargs):
        return -self._parameterized_logp(*args, result=self._sampled, **kwargs)

    def _parameterized_entropy(self, *args, **kwargs):
        kwargs.update(self.inputs_nonrandom)
        return self._entropy(*args, **kwargs)
    
    def _expected_logp(self, **kwargs):
        # default implementation: compute E_q[ log p(x) ] as a Monte Carlo sample.
        samples = {}
        for (q_key, qdist) in kwargs.items():
            assert(q_key.startswith("q_"))
            key = q_key[2:]
            samples[key] = qdist._sampled
        return self._parameterized_logp(**samples)

    def _optimized_params(self, sess, feed_dict=None):
        optimized_params = {}
        return optimized_params

    def expected_logp(self):
        input_qs = {"q_"+name: node.q_distribution() for (name,node) in self.inputs_random.items()}
        q = self.q_distribution()
        with tf.name_scope(self.name + "_Elogp") as scope:
            expected_lp = self._expected_logp(q_result = q, **input_qs)
        return expected_lp

    def entropy(self):
        return self._sampled_entropy

    def derived_parameters(self, **input_vals):
        return {}
        
    def q_distribution(self):
        if self._q_distribution is None:
            default_q = self.default_q()
            
            # explicitly use the superclass method since some subclasses
            # may redefine attach_q to prevent user-attached q's
            ConditionalDistribution.attach_q(self, default_q)

        return self._q_distribution

    def attach_q(self, q_distribution):

        if self._q_distribution is not None:
            raise Exception("trying to attach Q distribution %s at %s, but another distribution %s is already attached!" % (self._q_distribution, self, self._q_distribution))

        assert(self.shape == q_distribution.shape)
        self._q_distribution = q_distribution
                                        
    def observe(self, observed_val):
        tf_value = tf.convert_to_tensor(observed_val)
        q_dist = WrapperNode(tf_value, name="observed_" + self.name)
        self.attach_q(q_dist)
        return q_dist
        
    
    def __str__(self):
        return repr(self)

    def __repr__(self):
        return str(self.__class__.__name__) + "_" + self.name

class WrapperNode(ConditionalDistribution):
    """
    Lifts a Tensorflow graph node representing a point value to a ConditionalDistribution. 
    This implements the 'return' operation of the probability monad. 
    """

    def __init__(self, tf_value=None, shape=None, **kwargs):

        if shape is None and tf_value is not None:
            shape = concrete_shape(tf_value.get_shape())
        super(WrapperNode, self).__init__(shape=shape, tf_value=tf_value, **kwargs)

        self.mean = self.tf_value
        self.variance = tf.zeros_like(self.mean, name="variance")
        
    def inputs(self):
        from parameterization import unconstrained
        return {"tf_value": unconstrained}

    def _input_shape(self, param, result, **kwargs):
        if param == "tf_value":
            return result
        else:
            raise NotImplementedError
    
    def _sample(self, tf_value):
        return tf_value

    def _logp(self, **kwargs):
        return tf.constant(0.0, dtype=tf.float32), {}

    def _entropy(self, **kwargs):
        return tf.constant(0.0, dtype=tf.float32)

