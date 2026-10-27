# Copyright 2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest

from dimos.control.task import (
    CoordinatorState,
    JointStateSnapshot,
)
from dimos.control.tasks.trajectory_task.trajectory_task import (
    JointTrajectoryTask,
    JointTrajectoryTaskConfig,
    TrajectoryExecutionStatus,
)
from dimos.msgs.sensor_msgs.JointState import JointState
from dimos.msgs.trajectory_msgs.JointTrajectory import JointTrajectory
from dimos.msgs.trajectory_msgs.TrajectoryPoint import TrajectoryPoint
from dimos.msgs.trajectory_msgs.TrajectoryStatus import TrajectoryState


@pytest.mark.parametrize("single_point", [False, True])
@pytest.mark.parametrize("preempted", [False, True])
def test_jtt_does_not_pull_joint_back_after_another_task_moves_it(single_point, preempted):
    task = JointTrajectoryTask(JointTrajectoryTaskConfig(joint_names=["joint"]))
    state = CoordinatorState(
        joints=JointStateSnapshot(joint_positions={"joint": 0.0}), t_now=0.1, dt=0.1
    )
    first_target = 1.0 if preempted else 0.1
    first = JointTrajectory(
        joint_names=["joint"], points=[TrajectoryPoint(positions=[first_target])]
    )
    assert task.execute(first, {}).status is TrajectoryExecutionStatus.ACCEPTED
    output = task.compute(state)
    assert output is not None
    assert output.positions == pytest.approx([0.1])
    if preempted:
        task.on_preempted("other_task", frozenset({"joint"}))
    assert not task.is_active()

    # Another task moves the joint away from JTT's previous command.
    state = CoordinatorState(
        joints=JointStateSnapshot(joint_positions={"joint": -1.0}), t_now=1.0, dt=0.01
    )
    assert task.compute(state) is None
    points = [TrajectoryPoint(positions=[-1.2])]
    if not single_point:
        points = [
            TrajectoryPoint(positions=[-1.0]),
            TrajectoryPoint(positions=[-1.2], time_from_start=0.1),
        ]
    assert (
        task.execute(
            JointTrajectory(joint_names=["joint"], points=points), state.joints.joint_positions
        ).status
        is TrajectoryExecutionStatus.ACCEPTED
    )

    # The new motion goes farther away from the old command. Every output
    # must move toward the new target, bounded from the new starting pose.
    previous = -1.0
    for tick in range(30):
        state.t_now = 1.0 + tick * state.dt
        output = task.compute(state)
        if output is None:
            break
        commanded = output.positions[0]
        assert -state.dt - 1e-9 <= commanded - previous <= 1e-9
        previous = commanded
    assert previous == pytest.approx(-1.2)
    assert not task.is_active()
    assert task.compute(state) is None


def test_completed_joint_reanchors_while_other_joint_keeps_command_continuity():
    task = JointTrajectoryTask(JointTrajectoryTaskConfig(joint_names=["finished", "running"]))
    measured = JointStateSnapshot(joint_positions={"finished": 0.0, "running": 0.0})
    initial = JointTrajectory(
        joint_names=["finished", "running"], points=[TrajectoryPoint(positions=[0.1, 1.0])]
    )
    assert task.execute(initial, {}).status is TrajectoryExecutionStatus.ACCEPTED
    output = task.compute(CoordinatorState(joints=measured, t_now=0.1, dt=0.1))
    assert output is not None
    assert output.positions == pytest.approx([0.1, 0.1])
    measured.joint_positions["finished"] = -1.0
    replacement = JointTrajectory(
        joint_names=["finished"],
        points=[
            TrajectoryPoint(positions=[-1.0]),
            TrajectoryPoint(positions=[0.1], time_from_start=2.0),
        ],
    )
    assert (
        task.execute(replacement, measured.joint_positions).status
        is TrajectoryExecutionStatus.ACCEPTED
    )
    output = task.compute(CoordinatorState(joints=measured, t_now=0.2, dt=0.1))
    assert output is not None
    assert output.positions == pytest.approx([-1.0, 0.2])


@pytest.mark.parametrize(
    "positions, expected",
    [
        ({}, TrajectoryExecutionStatus.START_STATE_UNAVAILABLE),
        ({"joint": -1.0}, TrajectoryExecutionStatus.START_STATE_MISMATCH),
    ],
)
def test_completed_trajectory_does_not_bypass_start_validation(positions, expected):
    task = JointTrajectoryTask(JointTrajectoryTaskConfig(joint_names=["joint"]))
    initial = JointTrajectory(joint_names=["joint"], points=[TrajectoryPoint(positions=[0.1])])
    assert task.execute(initial, {}).status is TrajectoryExecutionStatus.ACCEPTED
    task.compute(
        CoordinatorState(
            joints=JointStateSnapshot(joint_positions={"joint": 0.0}), t_now=0.1, dt=0.1
        )
    )
    trajectory = JointTrajectory(
        joint_names=["joint"],
        points=[
            TrajectoryPoint(positions=[0.1]),
            TrajectoryPoint(positions=[1.0], time_from_start=2.0),
        ],
    )
    assert task.execute(trajectory, positions).status is expected


def _ramp() -> JointTrajectory:
    return JointTrajectory(
        joint_names=["joint"],
        points=[
            TrajectoryPoint(positions=[0.0]),
            TrajectoryPoint(positions=[1.0], time_from_start=2.0),
        ],
    )


@pytest.mark.parametrize("keepalive", [False, True])
def test_deadman_aborts_trajectory_when_keepalive_stops(keepalive):
    task = JointTrajectoryTask(JointTrajectoryTaskConfig(joint_names=["joint"]))
    joints = JointStateSnapshot(joint_positions={"joint": 0.0})
    result = task.execute(_ramp(), joints.joint_positions, deadman_timeout=0.5)
    assert result.status is TrajectoryExecutionStatus.ACCEPTED
    t_now = 0.0
    for tick in range(1, 101):
        t_now = tick * 0.01
        if keepalive:
            task.keepalive(t_now)
        if task.compute(CoordinatorState(joints=joints, t_now=t_now, dt=0.01)) is None:
            break
    if keepalive:
        assert task.get_state() is TrajectoryState.EXECUTING
        return
    assert task.get_state() is TrajectoryState.ABORTED
    assert 0.5 < t_now <= 0.53
    # The next execution without a deadman runs unsupervised again.
    assert (
        task.execute(_ramp(), joints.joint_positions).status is TrajectoryExecutionStatus.ACCEPTED
    )
    for tick in range(1, 101):
        task.compute(CoordinatorState(joints=joints, t_now=1.0 + tick * 0.01, dt=0.01))
    assert task.get_state() is TrajectoryState.EXECUTING


def test_estop_aborts_trajectory_and_latches_until_cleared():
    task = JointTrajectoryTask(JointTrajectoryTaskConfig(joint_names=["joint"]))
    joints = JointStateSnapshot(joint_positions={"joint": 0.0})
    assert (
        task.execute(_ramp(), joints.joint_positions).status is TrajectoryExecutionStatus.ACCEPTED
    )
    assert task.compute(CoordinatorState(joints=joints, t_now=0.01, dt=0.01)) is not None

    task.set_estop(True)
    assert task.get_state() is TrajectoryState.ABORTED
    assert task.compute(CoordinatorState(joints=joints, t_now=0.02, dt=0.01)) is None
    result = task.execute(_ramp(), joints.joint_positions)
    assert result.status is TrajectoryExecutionStatus.ESTOPPED
    assert not task.on_joint_command(JointState(name=["joint"], position=[0.5]), 0.03)
    assert not task.is_active()

    task.set_estop(False)
    assert (
        task.execute(_ramp(), joints.joint_positions).status is TrajectoryExecutionStatus.ACCEPTED
    )


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_deadman_timeout_is_rejected(timeout):
    task = JointTrajectoryTask(JointTrajectoryTaskConfig(joint_names=["joint"]))
    result = task.execute(_ramp(), {"joint": 0.0}, deadman_timeout=timeout)
    assert result.status is TrajectoryExecutionStatus.INVALID_TRAJECTORY
    assert not task.is_active()
