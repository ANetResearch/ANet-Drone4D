/*
 * D4dWind component (M24 §6.5.8 field wind mode): per-vehicle wind vector written on the model entity by Drone4DClock in
 * PreUpdate (priority -200) from Control.wind (WindSp, zero-order hold from apply_step on) and read by Drone4DWindDrag in
 * the same iteration. ENU m/s, the direction the air moves to (M07 convention). Lives in libDrone4DGz.so with the other
 * Drone4D components, so it is registered once. Apache-2.0.
 */
#ifndef D4D_WIND_HH_
#define D4D_WIND_HH_

#include <gz/sim/components/Component.hh>
#include <gz/sim/components/Factory.hh>
#include <gz/sim/components/Serialization.hh>
#include <gz/sim/config.hh>

namespace drone4d
{
struct WindCmd
{
  double wx{0}, wy{0}, wz{0};
  bool operator==(const WindCmd &_o) const { return wx == _o.wx && wy == _o.wy && wz == _o.wz; }
  bool operator!=(const WindCmd &_o) const { return !(*this == _o); }
};
}  // namespace drone4d

namespace gz::sim
{
inline namespace GZ_SIM_VERSION_NAMESPACE
{
namespace components
{
using D4dWind = Component<drone4d::WindCmd, class D4dWindTag>;
GZ_SIM_REGISTER_COMPONENT("drone4d_components.D4dWind", D4dWind)
}  // namespace components
}  // namespace GZ_SIM_VERSION_NAMESPACE
}  // namespace gz::sim

#endif
