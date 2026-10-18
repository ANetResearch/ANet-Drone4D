/*
 * Drone4D gz components shared by Drone4DClock and Drone4DVelocityControl (M24 §6.5.2, §6.5.4). Both systems live in the
 * same library (libDrone4DGz.so), so the component is registered once. Apache-2.0.
 */
#ifndef D4D_COMPONENTS_HH_
#define D4D_COMPONENTS_HH_

#include <gz/sim/components/Component.hh>
#include <gz/sim/components/Factory.hh>
#include <gz/sim/components/Serialization.hh>
#include <gz/sim/config.hh>

namespace drone4d
{
/// Velocity setpoint written by Drone4DClock in PreUpdate (priority -200) and read by Drone4DVelocityControl in the same
/// iteration: ENU m/s, yaw rate rad/s, enabled = false stops the rotors.
struct VelocityCmd
{
  double vx{0}, vy{0}, vz{0}, yawRate{0};
  bool enabled{true};
  bool operator==(const VelocityCmd &_o) const
  {
    return vx == _o.vx && vy == _o.vy && vz == _o.vz && yawRate == _o.yawRate && enabled == _o.enabled;
  }
  bool operator!=(const VelocityCmd &_o) const { return !(*this == _o); }
};
}  // namespace drone4d

namespace gz::sim
{
inline namespace GZ_SIM_VERSION_NAMESPACE
{
namespace components
{
using D4dVelocityCmd = Component<drone4d::VelocityCmd, class D4dVelocityCmdTag>;
GZ_SIM_REGISTER_COMPONENT("drone4d_components.D4dVelocityCmd", D4dVelocityCmd)
}  // namespace components
}  // namespace GZ_SIM_VERSION_NAMESPACE
}  // namespace gz::sim

#endif
