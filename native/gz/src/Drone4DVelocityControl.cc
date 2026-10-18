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
 * Drone4D modifications (M24 §6.5.4): commands come from the D4dVelocityCmd component written by Drone4DClock in the
 * same iteration instead of gz-transport topics; world-frame commands are expressed in the current body frame; an
 * enabled = false command stops the rotors; init_hover starts airborne vehicles at the hover command.
 */

#include <gz/msgs/actuators.pb.h>
#include <gz/msgs/twist.pb.h>

#include <limits>

#include <gz/common/Profiler.hh>

// One library, two systems: the plugin hook is defined in Drone4DClock.cc (Register.hh); here only RegisterMore.hh.
#include <gz/plugin/RegisterMore.hh>
#include <gz/transport/Node.hh>

#include <gz/math/Inertial.hh>
#include <gz/math/Vector3.hh>

#include <gz/math/eigen3/Conversions.hh>

#include <sdf/sdf.hh>

#include "gz/sim/components/Actuators.hh"
#include "gz/sim/components/Gravity.hh"
#include "gz/sim/components/Inertial.hh"
#include "gz/sim/components/Link.hh"
#include "gz/sim/components/Model.hh"
#include "gz/sim/components/ParentEntity.hh"
#include "gz/sim/components/World.hh"
#include "gz/sim/Link.hh"
#include "gz/sim/Model.hh"

#include "D4dComponents.hh"
#include "Drone4DVelocityControl.hh"


using namespace gz;
using namespace sim;
using namespace systems;
using namespace d4d_multicopter_control;

//////////////////////////////////////////////////
void Drone4DVelocityControl::Configure(const Entity &_entity,
    const std::shared_ptr<const sdf::Element> &_sdf,
    EntityComponentManager &_ecm,
    EventManager &/*_eventMgr*/)
{
  this->model = Model(_entity);

  if (!this->model.Valid(_ecm))
  {
    gzerr << "Drone4DVelocityControl plugin should be attached to a model "
           << "entity. Failed to initialize." << std::endl;
    return;
  }

  auto sdfClone = _sdf->Clone();

  if (sdfClone->HasElement("comLinkName"))
  {
    this->comLinkName = sdfClone->Get<std::string>("comLinkName");
  }

  if (this->comLinkName.empty())
  {
    gzerr << "found an empty comLinkName parameter. Failed to initialize.\n";
    return;
  }

  // Get the link entity
  this->comLinkEntity = this->model.LinkByName(_ecm, this->comLinkName);

  if (this->comLinkEntity == kNullEntity)
  {
    gzerr << "Link " << this->comLinkName
           << " could not be found. Failed to initialize.\n";
    return;
  }

  createFrameDataComponents(_ecm, this->comLinkEntity);

  VehicleParameters vehicleParams;

  math::Inertiald vehicleInertial;
  vehicleInertial = this->VehicleInertial(_ecm, this->model.Entity());

  vehicleParams.mass = vehicleInertial.MassMatrix().Mass();
  vehicleParams.inertia = math::eigen3::convert(vehicleInertial.Moi());
  if (sdfClone->HasElement("rotorConfiguration"))
  {
    vehicleParams.rotorConfiguration =
        loadRotorConfiguration(_ecm, sdfClone->GetElement("rotorConfiguration"),
                               this->model, this->comLinkEntity);
    // DEBUG
    // std::cout << "Found "
    //           << vehicleParams.rotorConfiguration.size()
    //           << " rotors" << std::endl;
    // for (const auto &rotor : vehicleParams.rotorConfiguration)
    // {
    //   std::cout << rotor.angle << " "
    //             << rotor.armLength << " " << rotor.forceConstant << " "
    //             << rotor.momentConstant << " " << rotor.direction << "\n\n";
    // }
  }
  else
  {
    gzerr << "Please specify rotorConfiguration.\n";
  }

  this->rotorVelocities.resize(vehicleParams.rotorConfiguration.size());

  auto worldEntity = _ecm.EntityByComponents(components::World());

  if (kNullEntity == worldEntity)
  {
    gzerr << "World entity missing." << std::endl;
    return;
  }

  // Get the world acceleration (defined in world frame)
  auto gravityComp = _ecm.Component<components::Gravity>(worldEntity);

  if (nullptr == gravityComp)
  {
    gzerr << "World missing gravity." << std::endl;
    return;
  }

  vehicleParams.gravity = math::eigen3::convert(gravityComp->Data());

  LeeVelocityControllerParameters controllerParameters;

  if (sdfClone->HasElement("velocityGain"))
  {
    controllerParameters.velocityGain =
        math::eigen3::convert(sdfClone->Get<math::Vector3d>("velocityGain"));
  }
  else
  {
    gzerr << "Please specify velocityGain for Drone4DVelocityControl.\n";
    return;
  }

  if (sdfClone->HasElement("attitudeGain"))
  {
    controllerParameters.attitudeGain =
        math::eigen3::convert(sdfClone->Get<math::Vector3d>("attitudeGain"));
  }
  else
  {
    gzerr << "Please specify attitudeGain for Drone4DVelocityControl.\n";
    return;
  }

  if (sdfClone->HasElement("angularRateGain"))
  {
    controllerParameters.angularRateGain =
        math::eigen3::convert(sdfClone->Get<math::Vector3d>("angularRateGain"));
  }
  else
  {
    gzerr << "Please specify angularRateGain Drone4DVelocityControl.\n";
    return;
  }

  if (sdfClone->HasElement("maximumLinearAcceleration"))
  {
    controllerParameters.maxLinearAcceleration = math::eigen3::convert(
        sdfClone->Get<math::Vector3d>("maximumLinearAcceleration"));
  }
  else
  {
    controllerParameters.maxLinearAcceleration.setConstant(
        std::numeric_limits<double>::max());
  }

  if (sdfClone->HasElement("maximumLinearVelocity"))
  {
    this->maximumLinearVelocity =
        sdfClone->Get<math::Vector3d>("maximumLinearVelocity").Abs();
  }
  else
  {
    this->maximumLinearVelocity.Set(
        std::numeric_limits<double>::max(),
        std::numeric_limits<double>::max(),
        std::numeric_limits<double>::max());
  }

  if (sdfClone->HasElement("maximumAngularVelocity"))
  {
    this->maximumAngularVelocity =
        sdfClone->Get<math::Vector3d>("maximumAngularVelocity").Abs();
  }
  else
  {
    this->maximumAngularVelocity.Set(
        std::numeric_limits<double>::max(),
        std::numeric_limits<double>::max(),
        std::numeric_limits<double>::max());
  }

  this->velocityController = LeeVelocityController::MakeController(
      controllerParameters, vehicleParams);

  if (nullptr == this->velocityController)
  {
    gzerr << "Error while creating the LeeVelocityController\n";
    return;
  }

  math::Vector3d linearVelocityMean{0, 0, 0};
  sdfClone->Get<math::Vector3d>("linearVelocityNoiseMean",
      linearVelocityMean, linearVelocityMean);

  math::Vector3d linearVelocityStdDev{0, 0, 0};
  sdfClone->Get<math::Vector3d>("linearVelocityNoiseStdDev",
      linearVelocityStdDev, linearVelocityStdDev);

  math::Vector3d angularVelocityMean{0, 0, 0};
  sdfClone->Get<math::Vector3d>("angularVelocityNoiseMean",
      angularVelocityMean, angularVelocityMean);

  math::Vector3d angularVelocityStdDev{0, 0, 0};
  sdfClone->Get<math::Vector3d>("angularVelocityNoiseStdDev",
      angularVelocityStdDev, angularVelocityStdDev);

  this->noiseParameters.linearVelocityMean =
      math::eigen3::convert(linearVelocityMean);
  this->noiseParameters.linearVelocityStdDev =
      math::eigen3::convert(linearVelocityStdDev);
  this->noiseParameters.angularVelocityMean =
      math::eigen3::convert(angularVelocityMean);
  this->noiseParameters.angularVelocityStdDev =
      math::eigen3::convert(angularVelocityStdDev);

  if (sdfClone->HasElement("robotNamespace"))
  {
    this->robotNamespace = transport::TopicUtils::AsValidTopic(
        sdfClone->Get<std::string>("robotNamespace"));
    if (this->robotNamespace.empty())
    {
      gzerr << "Robot namespace ["
             << sdfClone->Get<std::string>("robotNamespace") <<"] is invalid."
             << std::endl;
      return;
    }
  }
  else
  {
    gzerr << "Please specify a robotNamespace.\n";
    return;
  }

  sdfClone->Get<std::string>("commandFrame", this->commandFrame,
      this->commandFrame);

  sdfClone->Get<bool>("init_hover", this->initHover, this->initHover);

  // Create the Actuators component to take control of rotor speeds
#if GOOGLE_PROTOBUF_VERSION >= 7035000
  this->rotorVelocitiesMsg.mutable_velocity()->resize(
      this->rotorVelocities.size(), 0);
#else
  this->rotorVelocitiesMsg.mutable_velocity()->Resize(
      this->rotorVelocities.size(), 0);
#endif

  _ecm.CreateComponent(this->model.Entity(),
                       components::Actuators(this->rotorVelocitiesMsg));

  this->initialized = true;
}

//////////////////////////////////////////////////
math::Inertiald Drone4DVelocityControl::VehicleInertial(
    const EntityComponentManager &_ecm, Entity _entity)
{
  math::Inertiald vehicleInertial;

  for (const Entity &link :
       _ecm.ChildrenByComponents(_entity, components::Link()))
  {
    auto inertial = _ecm.Component<components::Inertial>(link);
    if (nullptr == inertial)
    {
      gzerr << "Could not find inertial component on link "
             << this->comLinkName << std::endl;
      return vehicleInertial;
    }
    vehicleInertial += inertial->Data();
  }

  for (const Entity &modelEnt :
       _ecm.ChildrenByComponents(_entity, components::Model()))
  {
    vehicleInertial += this->VehicleInertial(_ecm, modelEnt);
  }
  return vehicleInertial;
}

//////////////////////////////////////////////////
void Drone4DVelocityControl::PreUpdate(
    const UpdateInfo &_info,
    EntityComponentManager &_ecm)
{
  GZ_PROFILE("Drone4DVelocityControl::PreUpdate");

  if (!this->initialized)
  {
    return;
  }

  // \TODO(anyone) Support rewind
  if (_info.dt < std::chrono::steady_clock::duration::zero())
  {
    gzwarn << "Detected jump back in time ["
           << std::chrono::duration<double>(_info.dt).count()
           << "s]. System may not work properly." << std::endl;
  }

  // Nothing left to do if paused.
  if (_info.paused)
  {
    return;
  }

  // Command for this iteration (Drone4DClock writes it earlier in the same iteration, priority -200)
  EigenTwist cmdVel;
  const auto *cmdComp = _ecm.Component<components::D4dVelocityCmd>(this->model.Entity());
  if (cmdComp == nullptr && !this->initHover)
  {
    return;
  }
  drone4d::VelocityCmd cmd = cmdComp ? cmdComp->Data() : drone4d::VelocityCmd{};
  if (!cmd.enabled)
  {
    if (this->rotorVelocities.squaredNorm() > 0)
    {
      this->rotorVelocities.setZero();
      this->PublishRotorVelocities(_ecm, this->rotorVelocities);
    }
    return;
  }
  {
    math::Vector3d linear(cmd.vx, cmd.vy, cmd.vz);
    linear.Min(this->maximumLinearVelocity);
    linear.Max(-this->maximumLinearVelocity);
    math::Vector3d angular(0, 0, cmd.yawRate);
    angular.Min(this->maximumAngularVelocity);
    angular.Max(-this->maximumAngularVelocity);
    cmdVel.linear = math::eigen3::convert(linear);
    cmdVel.angular = math::eigen3::convert(angular);
  }

  std::optional<FrameData> frameData =
      getFrameData(_ecm, this->comLinkEntity, this->noiseParameters);
  if (!frameData.has_value())
  {
    // Errors would have already been printed
    return;
  }

  // Drone4D: upstream LeeVelocityController interprets the commanded linear
  // velocity in the full body frame (R * v_cmd, roll and pitch included), so a
  // tilted vehicle converts part of a horizontal command into a vertical one.
  // Express world / heading-frame commands in the current body frame so that
  // R * v_cmd equals the intended velocity at every physics step.
  if (this->commandFrame != "body")
  {
    const Eigen::Matrix3d R = frameData->pose.linear();
    Eigen::Vector3d vWorld = cmdVel.linear;
    if (this->commandFrame == "heading")
    {
      const double yaw = std::atan2(R(1, 0), R(0, 0));
      vWorld = Eigen::AngleAxisd(yaw, Eigen::Vector3d::UnitZ()) * cmdVel.linear;
    }
    cmdVel.linear = R.transpose() * vWorld;
  }

  this->velocityController->CalculateRotorVelocities(*frameData, cmdVel,
                                                     this->rotorVelocities);

  this->PublishRotorVelocities(_ecm, this->rotorVelocities);
}

//////////////////////////////////////////////////
void Drone4DVelocityControl::PublishRotorVelocities(
    EntityComponentManager &_ecm,
    const Eigen::VectorXd &_vels)
{
  if (_vels.size() != this->rotorVelocitiesMsg.velocity_size())
  {
#if GOOGLE_PROTOBUF_VERSION >= 7035000
    this->rotorVelocitiesMsg.mutable_velocity()->resize(_vels.size(), 0);
#else
    this->rotorVelocitiesMsg.mutable_velocity()->Resize(_vels.size(), 0);
#endif
  }
  for (int i = 0; i < this->rotorVelocities.size(); ++i)
  {
    this->rotorVelocitiesMsg.set_velocity(i, _vels(i));
  }
  // Publish the message by setting the Actuators component on the model entity.
  // This assumes that the MulticopterMotorModel system is attached to this
  // model
  auto actuatorMsgComp =
      _ecm.Component<components::Actuators>(this->model.Entity());

  if (actuatorMsgComp)
  {
    auto compFunc = [](const msgs::Actuators &_a, const msgs::Actuators &_b)
    {
      return std::equal(_a.velocity().begin(), _a.velocity().end(),
                        _b.velocity().begin());
    };
    auto state = actuatorMsgComp->SetData(this->rotorVelocitiesMsg, compFunc)
                     ? ComponentState::PeriodicChange
                     : ComponentState::NoChange;
    _ecm.SetChanged(this->model.Entity(), components::Actuators::typeId, state);
  }
  else
  {
    _ecm.CreateComponent(this->model.Entity(),
                         components::Actuators(this->rotorVelocitiesMsg));
  }
}

GZ_ADD_PLUGIN(Drone4DVelocityControl,
                    System,
                    Drone4DVelocityControl::ISystemConfigure,
                    Drone4DVelocityControl::ISystemPreUpdate)

GZ_ADD_PLUGIN_ALIAS(
    Drone4DVelocityControl,
    "drone4d::Drone4DVelocityControl")
