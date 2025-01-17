import math
from selfdrive.controls.lib.pid import PIDController
from common.numpy_fast import interp
from selfdrive.controls.lib.latcontrol import LatControl, MIN_STEER_SPEED
from selfdrive.controls.lib.vehicle_model import ACCELERATION_DUE_TO_GRAVITY
from cereal import log
from common.params import Params
from decimal import Decimal

# At higher speeds (25+mph) we can assume:
# Lateral acceleration achieved by a specific car correlates to
# torque applied to the steering rack. It does not correlate to
# wheel slip, or to speed.

# This controller applies torque to achieve desired lateral
# accelerations. To compensate for the low speed effects we
# use a LOW_SPEED_FACTOR in the error. Additionally there is
# friction in the steering wheel that needs to be overcome to
# move it at all, this is compensated for too.


FRICTION_THRESHOLD = 0.2


class LatControlTorque(LatControl):
  def __init__(self, CP, CI):
    super().__init__(CP, CI)
    self.pid = PIDController(CP.lateralTuning.torque.kp, CP.lateralTuning.torque.ki,
                            k_f=CP.lateralTuning.torque.kf, pos_limit=1.0, neg_limit=-1.0)
    self.get_steer_feedforward = CI.get_steer_feedforward_function()
    self.steer_max = 1.0
    self.pid.pos_limit = self.steer_max
    self.pid.neg_limit = -self.steer_max
    self.use_steering_angle = CP.lateralTuning.torque.useSteeringAngle
    self.friction = CP.lateralTuning.torque.friction

    self.params = Params()
    self.mpc_frame = 0
    self.custom_tune = False
    self.custom_tune_timer = 0

  def reset(self):
    super().reset()
    self.pid.reset()

  def live_tune(self, CP):
    self.mpc_frame += 1
    if self.mpc_frame % 300 == 0:
      self._torque_max_lat_accel = float(Decimal(self.params.get("TorqueMaxLatAccel", encoding="utf8")) * Decimal('0.1'))
      self._torque_use_steering_angle = self.params.get_bool("TorqueUseSteeringAngle")
      self._torque_friction = float(Decimal(self.params.get("TorqueFriction", encoding="utf8")) * Decimal('0.01'))
      self.use_steering_angle = self._torque_use_steering_angle
      self.friction = self._torque_friction
      self.pid = PIDController(2.0 / self._torque_max_lat_accel, 1.0 / self._torque_max_lat_accel,
                               k_f=(0.5 / self._torque_max_lat_accel), pos_limit=1.0, neg_limit=-1.0)
      self.mpc_frame = 0

  def update(self, active, CS, CP, VM, params, last_actuators, desired_curvature, desired_curvature_rate, llk):
    self.custom_tune_timer += 1
    if self.custom_tune_timer > 100:
      self.custom_tune_timer = 0
      self.custom_tune = self.params.get_bool("CustomLateralTune")
    if self.custom_tune:
      self.live_tune(CP)

    pid_log = log.ControlsState.LateralTorqueState.new_message()

    if CS.vEgo < MIN_STEER_SPEED or not active:
      output_torque = 0.0
      pid_log.active = False
    else:
      if self.use_steering_angle:
        actual_curvature = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
      else:
        actual_curvature_vm = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
        actual_curvature_llk = llk.angularVelocityCalibrated.value[2] / CS.vEgo
        actual_curvature = interp(CS.vEgo, [2.0, 5.0], [actual_curvature_vm, actual_curvature_llk])
      desired_lateral_accel = desired_curvature * CS.vEgo**2
      desired_lateral_jerk = desired_curvature_rate * CS.vEgo**2
      actual_lateral_accel = actual_curvature * CS.vEgo**2

      low_speed_factor = interp(CS.vEgo, [0, 15], [500, 0])
      setpoint = desired_lateral_accel + low_speed_factor * desired_curvature
      measurement = actual_lateral_accel + low_speed_factor * actual_curvature
      error = setpoint - measurement
      pid_log.error = error

      ff = desired_lateral_accel - params.roll * ACCELERATION_DUE_TO_GRAVITY
      freeze_integrator = CS.steeringRateLimited or CS.steeringPressed or CS.vEgo < 5
      output_torque = self.pid.update(error,
                                      feedforward=ff,
                                      speed=CS.vEgo,
                                      freeze_integrator=freeze_integrator)

      friction_compensation = interp(error, [-FRICTION_THRESHOLD, FRICTION_THRESHOLD], [-self.friction, self.friction])
      output_torque += friction_compensation

      pid_log.active = True
      pid_log.p = self.pid.p
      pid_log.i = self.pid.i
      pid_log.d = self.pid.d
      pid_log.f = self.pid.f
      pid_log.output = -output_torque
      pid_log.saturated = self._check_saturation(self.steer_max - abs(output_torque) < 1e-3, CS)

    #TODO left is positive in this convention
    return -output_torque, 0.0, pid_log