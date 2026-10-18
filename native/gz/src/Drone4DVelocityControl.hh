/*
 * Copyright (C) 2019 Open Source Robotics Foundation
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */
#ifndef D4D_VC_HH_
#define D4D_VC_HH_

#include <Eigen/Geometry>
#include <memory>
#include <string>

#include <gz/msgs/actuators.pb.h>
#include <gz/msgs/boolean.pb.h>
#include <gz/msgs/twist.pb.h>
#include <gz/transport/Node.hh>

#include <gz/sim/System.hh>
#include "gz/sim/Link.hh"
#include "gz/sim/Model.hh"

#include "Common.hh"
#include "LeeVelocityController.hh"

namespace gz
{
namespace sim
{
// Inline bracket to help doxygen filtering.
inline namespace GZ_SIM_VERSION_NAMESPACE {
namespace systems
{
  /// \brief This is a velocity controller for multicopters that allows control
  /// over the linear velocity and the yaw angular velocity of the vehicle. The
  /// velocities are expressed in the body frame of the vehicle. A vehicle with
  /// at least 4 rotors is required for the controller to function.
  ///
  /// Note that this system only computes the necessary rotor velocities and
  /// updates the Actuators component on the model entity. To actually convert
  /// these velocities to thrust, this system requires the MulticopterMotorModel
  /// system on each rotor. Note also that only one Drone4DVelocityControl
  /// system is allowed per model.
  ///
  /// This system is inspired by the LeePositionController from RotorS
  /// https://github.com/ethz-asl/rotors_simulator/blob/master/rotors_control/include/rotors_control/lee_position_controller.h
  /// Instead of subscribing to odometry messages, this system uses ground truth
  /// values of orientation, linear velocity and angular velocity from
  /// simulation. As such it is not designed to work with user supplied state
  /// estimators. Instead, the system allows noise to be added to velocity
  /// values to simulate the behavior of state estimators.
  ///
  /// Main differences/additions include:
  ///  * Only controls velocity. Velocity estimates are obtained from simulation
  ///  ground truth, but noise can be added to simulate realistic estimators.
  ///  * Automatic calculation of rotor angle and arm length
  ///  * Automatic calculation of vehicle mass and inertia
  ///  * Handling of non-orthogonal rotor axis
  ///  * Maximum acceleration limit
  ///  * Can be enabled/disabled at runtime.
  ///
  /// ## System Parameters
  ///
  /// - `robotNamespace`: Name used in log messages. Required.
  ///
  /// - Commands (Drone4D): the drone4d D4dVelocityCmd component on the model entity, written by Drone4DClock in the same
  /// iteration (ENU velocity, yaw rate, enabled). enabled = false stops the rotors.
  ///
  /// - `init_hover` (bool, default true): before the first command, hold a zero-velocity (hover) command.
  ///
  /// - `comLinkName`: The link associated with the center of mass of the
  /// vehicle. That is, the origin of the center of mass may not be on this
  /// link, but this link and the center of mass frame have a fixed transform.
  /// Almost always this should be the base_link of the vehicle. This is a
  /// required parameter.
  ///
  /// - `velocityGain` (x, y, z): Proportional gain on linear velocity.
  /// attitudeGain (roll, pitch, yaw): Proportional gain on attitude. This
  /// parameter is scaled by the inverse of the inertia matrix so two vehicles
  /// with different inertial characteristics may have the same gain if other
  /// parameters, such as the forceConstant, are kept the same. This is a
  /// required parameter.
  ///
  /// - `angularRateGain` (roll, pitch, yaw): Proportional gain on angular
  /// velocity. Even though only the yaw angle velocity is controlled, proper
  /// gain values for roll and pitch velocities must be specified. This
  /// parameter is scaled by the inverse of the inertia matrix so two vehicles
  /// with different inertial characteristics may have the same gain if other
  /// parameters, such as the forceConstant, are kept the same. This is a
  /// required parameter.
  ///
  /// - `maxLinearAcceleration` (x, y, z): Maximum limit on linear acceleration.
  /// The default value is DBL_MAX.
  ///
  /// - `maximumLinearVelocity` (x, y, z): Maximum commanded linear velocity.
  /// The default value is DBL_MAX.
  ///
  /// - `maximumAngularVelocity` (roll, pitch, yaw): Maximum commanded angular
  /// velocity. The default value is DBL_MAX.
  ///
  /// - `linearVelocityNoiseMean` (x, y, z): Mean of Gaussian noise on linear
  /// velocity values obtained from simulation. The default value is (0, 0, 0).
  ///
  /// - `linearVelocityNoiseStdDev` (x, y, z): Standard deviation of Gaussian
  /// noise on linear values obtained from simulation. A value of 0 implies
  /// noise is NOT applied to the component. The default value is (0, 0, 0).
  ///
  /// - `angularVelocityNoiseMean` (roll, pitch, yaw): Mean of Gaussian noise on
  /// angular velocity values obtained from simulation. The default value is (0,
  /// 0, 0).
  ///
  /// - `angularVelocityNoiseStdDev` (roll, pitch, yaw): Standard deviation of
  /// gaussian noise on angular velocity values obtained from simulation. A
  /// value of 0 implies noise is NOT applied to the component. The default
  /// value is (0, 0, 0).
  ///
  /// - `rotorConfiguration`: This contains a list of `<rotor>` elements for
  /// each rotor in the vehicle. This is a required parameter.
  ///
  ///    - `rotor`: Contains information about a rotor in the vehicle. All the
  ///    elements of `<rotor>` are required parameters.
  ///
  ///      - `jointName`: The name of the joint associated with this rotor.
  ///
  ///      - `forceConstant`: A constant that multiplies with the square of the
  ///      rotor's velocity to compute its thrust.
  ///
  ///      - `momentConstant`: A constant the multiplies with the rotor's
  ///      thrust to compute its moment.
  ///
  ///      - `direction`: Direction of rotation of the rotor. +1 is
  ///      counterclockwise and -1 is clockwise.
  ///
  /// ## Examples
  ///
  /// See examples/worlds/quadcopter.sdf for a demonstration.
  ///
  class Drone4DVelocityControl
      : public System,
        public ISystemConfigure,
        public ISystemPreUpdate
  {
    /// \brief Constructor
    public: Drone4DVelocityControl() = default;

    // Documentation inherited
    public: void Configure(const Entity &_entity,
                           const std::shared_ptr<const sdf::Element> &_sdf,
                           EntityComponentManager &_ecm,
                           EventManager &_eventMgr) override;

    // Documentation inherited
    public: void PreUpdate(
                const gz::sim::UpdateInfo &_info,
                gz::sim::EntityComponentManager &_ecm) override;

    /// \brief Callback for twist messages
    /// The controller waits for the first twist message before publishing any
    /// rotor velocities.

    /// \brief Callback for enable messages
    /// \param[in] _msg Callback message. If false, the controller sends a zero
    /// rotor velocity command once and gets disabled. If the vehicle is in the
    /// air, disabling the controller will cause it to fall. If true, the

    /// \brief Publish provided rotor velocities
    /// \param[in] _ecm Mutable reference to the EntityComponentManager
    /// \param[in] _vels Rotor velocities to be published
    private: void PublishRotorVelocities(
                 gz::sim::EntityComponentManager &_ecm,
                 const Eigen::VectorXd &_vels);

    /// \brief Get the vehicle inertial from child links and nested models
    /// \param[in] _ecm Immutable reference to the EntityComponentManager
    /// \param[in] _entity Model entity to get inertial for
    private: math::Inertiald VehicleInertial(
                 const EntityComponentManager &_ecm,
                 Entity _entity);

    /// \brief Model interface
    private: Model model{kNullEntity};

    /// \brief Link name
    private: std::string comLinkName;

    /// \brief Link Entity
    private: Entity comLinkEntity;

    /// \brief Namespace used in log messages.
    private: std::string robotNamespace;

    /// \brief Frame of the commanded linear velocity: world | heading | body.
    private: std::string commandFrame{"world"};

    /// \brief Holds the rotor velocities computed by the controller. This is
    /// here so we don't need to allocate memory every simulation step.
    private: Eigen::VectorXd rotorVelocities;

    /// \brief Velocity controller
    private: std::unique_ptr<d4d_multicopter_control::LeeVelocityController>
                 velocityController;

    /// \brief Noise parameters read from SDF
    private: d4d_multicopter_control::NoiseParameters noiseParameters;

    /// \brief Maximum commanded linear velocity
    private: math::Vector3d maximumLinearVelocity;

    /// \brief Maximum commanded angular velocity
    private: math::Vector3d maximumAngularVelocity;

    /// \brief Rotor velocities message. This is here so we don't allocate
    /// memory every time we publish a message.
    private: msgs::Actuators rotorVelocitiesMsg;

    /// \brief Whether the system has been initialized with valid parameters
    private: bool initialized{false};

    /// \brief Start airborne vehicles at the hover command until the first setpoint arrives
    private: bool initHover{true};
  };
  }
}
}
}

#endif
