import unittest

import paddle.v2.framework.layers as layers
from paddle.v2.framework.framework import Program
from paddle.v2.framework.executor import Executor
from paddle.v2.framework.backward import append_backward_ops
import numpy as np
import paddle.v2.framework.core as core


class PyRNNBase(object):
    def __init__(self, input_shape, output_shape):
        self.x = np.ones(shape=input_shape).astype("float32")
        self.y = np.zeros(shape=output_shape).astype("float32")

    def step(self, step_id, x):
        raise NotImplementedError

    def forward(self):
        for step_id in range(self.x.shape[0]):
            self.step(step_id, self.x[step_id])
        return np.array([np.mean(self.y)])

    def segment_inputs(self):
        return [self.x[i] for i in range(self.x.shape[0])]


class PySimpleRNN1(PyRNNBase):
    def __init__(self, input_shape, output_shape):
        super(PySimpleRNN1, self).__init__(input_shape, output_shape)

        seq_len, batch_size, input_dim = input_shape
        self.h_boot = np.random.normal(size=(batch_size,
                                             input_dim)).astype("float32")

        self.scale = 1.0 / 2.0
        men_dim = (seq_len, batch_size, input_dim)
        self.mems = np.zeros(shape=men_dim).astype("float32")

    def step(self, step_id, x):
        if step_id == 0:
            pre_mem = self.h_boot
        else:
            pre_mem = self.mems[step_id - 1]
        self.mems[step_id] = (pre_mem + x) * self.scale
        self.y[step_id] = self.mems[step_id]


class PySimpleRNN2(PyRNNBase):
    def __init__(self, input_shape, output_shape):
        super(PySimpleRNN2, self).__init__(input_shape, output_shape)

        seq_len, batch_size, input_dim = input_shape
        self.W = np.random.normal(size=(input_dim, input_dim)).astype("float32")
        self.U = np.random.normal(size=(input_dim, input_dim)).astype("float32")
        self.h_boot = np.ones(shape=(batch_size, input_dim)).astype("float32")

        men_dim = (seq_len, batch_size, input_dim)
        self.mems = np.zeros(shape=men_dim).astype("float32")

    def step(self, step_id, x):
        if step_id > 0:
            pre_mem = self.mems[step_id - 1]
        else:
            pre_mem = self.h_boot
        xW = np.matmul(x, self.W).astype("float32")
        hU = np.matmul(pre_mem, self.U).astype("float32")

        def py_sigmoid(x):
            return 1. / (1. + np.exp(-x))

        self.mems[step_id] = py_sigmoid(xW + hU)
        self.y[step_id] = self.mems[step_id]


def create_tensor(np_data, place):
    tensor = core.LoDTensor()
    tensor.set(np_data, place)
    return tensor


class RecurrentOpTest1(unittest.TestCase):
    '''
    Test RNNOp
    equation:
        h_t = ( x_t + h_{t-1} ) / scale
    vars:
        - x
    memories:
        - h
    outputs:
        - h
    '''

    input_dim = 2
    batch_size = 1
    sent_len = 1

    def setup_program(self):
        self.main_program = Program()
        self.startup_program = Program()
        self.p_info = {
            "main_program": self.main_program,
            "startup_program": self.startup_program
        }
        self.place = core.CPUPlace()

    def setUp(self):
        self.setup_program()
        self.data_field = {"x", "h_boot"}

        self.input_shape = (self.sent_len, self.batch_size, self.input_dim)
        self.output_shape = (self.sent_len, self.batch_size, self.input_dim)
        self.py_rnn = PySimpleRNN1(self.input_shape, self.output_shape)

        self.output = layers.mean(x=self.create_rnn_op(), **self.p_info)

    def create_rnn_op(self):
        x = layers.data(
            shape=[self.sent_len, self.batch_size, self.input_dim],
            data_type='float32',
            name='x',
            append_batch_size=False,
            **self.p_info)
        x.stop_gradient = False
        h_boot = layers.data(
            shape=[self.input_dim],
            data_type='float32',
            name='h_boot',
            **self.p_info)
        h_boot.stop_gradient = False

        rnn = layers.StaticRNN(main_program=self.main_program)
        with rnn.step():
            h_pre = rnn.memory(init=h_boot)
            x_t = rnn.step_input(x)

            h = layers.scale(
                x=layers.elementwise_add(
                    x=h_pre, y=x_t, **self.p_info),
                scale=self.py_rnn.scale,
                **self.p_info)

            rnn.update_memory(h_pre, h)
            rnn.output(h)

        return rnn()

    def forward(self):
        self.feed_map = {
            x: create_tensor(getattr(self.py_rnn, x), self.place)
            for x in self.data_field
        }
        exe = Executor(self.place)
        out = exe.run(self.main_program,
                      feed=self.feed_map,
                      fetch_list=[self.output])

        return np.array(out[0])

    def backward(self):
        self.feed_map = {
            x: create_tensor(getattr(self.py_rnn, x), self.place)
            for x in self.data_field
        }
        fetch_list = [
            self.main_program.global_block().var(x + "@GRAD")
            for x in self.data_field
        ]

        exe = Executor(self.place)
        return exe.run(self.main_program,
                       feed=self.feed_map,
                       fetch_list=fetch_list)

    def test_backward(self):
        self.check_forward()

        append_backward_ops(self.output)

        ana_grad = [np.array(x) for x in self.backward()]

        num_grad = self.get_numerical_gradient()
        for idx, name in enumerate(self.data_field):
            self.assertEqual(num_grad[idx].shape, ana_grad[idx].shape)
            self.assertTrue(
                np.isclose(
                    num_grad[idx], ana_grad[idx], rtol=0.1).all())

    def check_forward(self):
        print 'test recurrent op forward'
        pd_output = self.forward()
        py_output = self.py_rnn.forward()
        print 'pd_output', pd_output
        print
        print 'py_output', py_output
        self.assertEqual(pd_output.shape, py_output.shape)
        self.assertTrue(np.isclose(pd_output, py_output, rtol=0.1).all())

    def get_numerical_gradient(self, delta=0.005):
        dloss_dout = 1.0
        feed_list = [getattr(self.py_rnn, x) for x in self.data_field]
        grad_list = [np.zeros_like(x) for x in feed_list]
        for feed, grad in zip(feed_list, grad_list):
            for f, g in np.nditer([feed, grad], op_flags=['readwrite']):
                o = float(f)
                f[...] = o + delta
                y_pos = self.forward()

                f[...] = o - delta
                y_neg = self.forward()

                f[...] = o
                dout_dfeed = (y_pos - y_neg) / (delta * 2)
                g[...] = dout_dfeed[0]

        return grad_list


class RecurrentOpTest2(RecurrentOpTest1):
    '''
    Test RNNOp
    equation:
        h_t = \sigma (W x_t + U h_{t-1})
    weights:
        - W
        - U
    vars:
        - x
    memories:
        - h
    outputs:
       - h
    '''

    input_dim = 2
    batch_size = 10
    sent_len = 2

    def setUp(self):
        self.setup_program()

        self.data_field = {"x", "h_boot", "W", "U"}

        self.input_shape = (self.sent_len, self.batch_size, self.input_dim)
        self.output_shape = (self.sent_len, self.batch_size, self.input_dim)
        self.py_rnn = PySimpleRNN2(self.input_shape, self.output_shape)

        self.output = layers.mean(x=self.create_rnn_op(), **self.p_info)

    def create_rnn_op(self):
        x = layers.data(
            shape=[self.sent_len, self.batch_size, self.input_dim],
            data_type='float32',
            name='x',
            append_batch_size=False,
            **self.p_info)
        x.stop_gradient = False
        h_boot = layers.data(
            shape=[self.input_dim],
            data_type='float32',
            name='h_boot',
            **self.p_info)
        h_boot.stop_gradient = False

        rnn = layers.StaticRNN(main_program=self.main_program)
        with rnn.step():
            h_pre = rnn.memory(init=h_boot)
            x_t = rnn.step_input(x)

            temp_l = layers.fc(input=x_t,
                               size=self.input_dim,
                               param_attr={'name': 'W'},
                               bias_attr=False,
                               **self.p_info)
            temp_r = layers.fc(input=h_pre,
                               size=self.input_dim,
                               param_attr={'name': 'U'},
                               bias_attr=False,
                               **self.p_info)

            h = layers.sigmoid(
                x=layers.elementwise_add(
                    x=temp_l, y=temp_r, **self.p_info),
                **self.p_info)

            rnn.update_memory(h_pre, h)
            rnn.output(h)

        return rnn()


class RecurrentOpMultipleMemoryTest(RecurrentOpTest1):
    '''
    Test RNNOp with two memories
    equation:
        h_1 = h_pre_1
        h_2 = h_pre_2
        y = h_1 + h_2
    vars:
        - x
    memories:
        - h_1, h_2
    outputs:
       - y
    '''

    class PySimpleRNN3(PyRNNBase):
        def __init__(self, input_shape, output_shape):
            super(RecurrentOpMultipleMemoryTest.PySimpleRNN3, self).__init__(
                input_shape, output_shape)

            seq_len, batch_size, input_dim = input_shape
            self.h_boot1 = np.random.normal(size=(batch_size,
                                                  input_dim)).astype("float32")
            self.h_boot2 = np.random.normal(size=(batch_size,
                                                  input_dim)).astype("float32")

            men_dim = (seq_len, batch_size, input_dim)
            self.mems1 = np.zeros(shape=men_dim).astype("float32")
            self.mems2 = np.zeros(shape=men_dim).astype("float32")

        def step(self, step_id, x):
            if step_id == 0:
                pre_mem1 = self.h_boot1
                pre_mem2 = self.h_boot2
            else:
                pre_mem1 = self.mems1[step_id - 1]
                pre_mem2 = self.mems2[step_id - 1]
            self.mems1[step_id] = pre_mem1
            self.mems2[step_id] = pre_mem2
            self.y[step_id] = self.mems1[step_id] + self.mems2[step_id] + x

    input_dim = 1
    batch_size = 1
    sent_len = 2

    def setUp(self):
        self.setup_program()

        self.data_field = {"x", "h_boot1", "h_boot2"}

        self.input_shape = (self.sent_len, self.batch_size, self.input_dim)
        self.output_shape = (self.sent_len, self.batch_size, self.input_dim)
        self.py_rnn = RecurrentOpMultipleMemoryTest.PySimpleRNN3(
            self.input_shape, self.output_shape)

        self.output = layers.mean(x=self.create_rnn_op(), **self.p_info)

    def create_rnn_op(self):
        x = layers.data(
            shape=[self.sent_len, self.batch_size, self.input_dim],
            data_type='float32',
            name='x',
            append_batch_size=False,
            **self.p_info)
        x.stop_gradient = False
        h_boot1 = layers.data(
            shape=[self.batch_size, self.input_dim],
            data_type='float32',
            name='h_boot1',
            append_batch_size=False,
            **self.p_info)
        h_boot1.stop_gradient = False
        h_boot2 = layers.data(
            shape=[self.batch_size, self.input_dim],
            data_type='float32',
            name='h_boot2',
            append_batch_size=False,
            **self.p_info)
        h_boot2.stop_gradient = False

        rnn = layers.StaticRNN(main_program=self.main_program)
        with rnn.step():
            h_pre1 = rnn.memory(init=h_boot1)
            h_pre2 = rnn.memory(init=h_boot2)
            x_t = rnn.step_input(x)

            mem1 = layers.scale(x=h_pre1, scale=1.0, **self.p_info)
            mem2 = layers.scale(x=h_pre2, scale=1.0, **self.p_info)
            out = layers.sums(input=[mem1, x_t, mem2], **self.p_info)

            rnn.update_memory(h_pre1, mem1)
            rnn.update_memory(h_pre2, mem2)
            rnn.output(out)

        return rnn()


class RecurrentOpNoMemBootTest(RecurrentOpTest1):
    '''
    Test RNNOp with two memories
    equation:
        mem = x + mem_pre
        y = mem
    vars:
        - x
    memories:
        - mem
    outputs:
       - y
    '''

    class PySimpleRNN4(PyRNNBase):
        def __init__(self, input_shape, output_shape):
            super(RecurrentOpNoMemBootTest.PySimpleRNN4, self).__init__(
                input_shape, output_shape)
            men_dim = input_shape
            self.mems = np.zeros(shape=men_dim).astype("float32")

        def step(self, step_id, x):
            if step_id == 0:
                pre_mem = np.zeros_like(x)
            else:
                pre_mem = self.mems[step_id - 1]
            self.mems[step_id] = pre_mem + x
            self.y[step_id] = self.mems[step_id]

    input_dim = 1
    batch_size = 1
    sent_len = 2

    def setUp(self):
        self.setup_program()

        self.data_field = {"x"}

        self.input_shape = (self.sent_len, self.batch_size, self.input_dim)
        self.output_shape = (self.sent_len, self.batch_size, self.input_dim)
        self.py_rnn = RecurrentOpNoMemBootTest.PySimpleRNN4(self.input_shape,
                                                            self.output_shape)
        self.output = layers.mean(x=self.create_rnn_op(), **self.p_info)
        print self.main_program

    def create_rnn_op(self):
        x = layers.data(
            shape=[self.sent_len, self.batch_size, self.input_dim],
            data_type='float32',
            name='x',
            append_batch_size=False,
            **self.p_info)
        x.stop_gradient = False

        rnn = layers.StaticRNN(main_program=self.main_program)
        with rnn.step():
            mem_pre = rnn.memory(shape=[-1, self.input_dim], batch_ref=x)
            x_t = rnn.step_input(x)
            mem = layers.elementwise_add(x=mem_pre, y=x_t, **self.p_info)
            rnn.update_memory(mem_pre, mem)
            rnn.output(mem)

        return rnn()


if __name__ == '__main__':
    unittest.main()
