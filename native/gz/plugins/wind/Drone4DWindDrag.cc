/*
 * Drone4DWindDrag (M24 §6.5.8 `field` wind mode, M24-FR-046; AWR-05 §5.3.1 item 3; D3-AC-35 tier comparison runs):
 * world-level gz-sim System that applies the per-vehicle wind force on base_link every iteration. The host samples the
 * M07 field per vehicle on the 20 ms environment grid (awr.phys.gz.wind.FieldWind), Drone4DClock writes the due
 * Control.wind entries into the D4dWind component of each model (PreUpdate, priority -200), this system reads it in the
 * same PreUpdate (default priority, after the clock) and adds the force before Physics integrates the step.
 *
 * Force laws (WindLaw.hh, numpy oracle awr.phys.gz.wind.drag_force):
 *   linear:    F = m k (w - v)
 *   composite: F = -1/2 rho CdA |v_r| v_r - sumOmega c_rd (x - (x . b3) b3), the FleetSim `aero = composite` law with the
 *              M21 parameters; sumOmega is the sum of the real rotor speeds (joint velocity x rotorVelocitySlowdownSim)
 *              and x = -w while MulticopterMotorModel already applies the rotor drag on the ground velocity
 *              (<motor_rotor_drag>true</motor_rotor_drag>, the default), so the total rotor drag acts on v - w.
 * No wind entity is created in the field mode, so WindEffects and the motor model see calm air.
 *
 * SDF parameters: <law> composite | linear, <rho> (kg/m^3, default 1.225), <motor_rotor_drag> (true),
 * <rotor_slowdown> (10), repeated <uav model="..." link="base_link" mass=".." cda=".." c_rd=".." k=".."
 * hover_sum_omega=".."> with child <rotor joint="rotor_0_joint"/> elements. hover_sum_omega is used while the rotor
 * joint velocities are not yet available (first iteration).
 *
 * Copyright 2026 Drone4D. Licensed under the Apache License, Version 2.0.
 */
#include <cmath>
#include <memory>
#include <string>
#include <vector>

#include <gz/common/Console.hh>
#include <gz/math/Pose3.hh>
#include <gz/math/Vector3.hh>
#include <gz/plugin/RegisterMore.hh>  // second plugin of libDrone4DGz.so: Drone4DClock.cc owns the Register.hh hook
#include <gz/sim/Joint.hh>
#include <gz/sim/Link.hh>
#include <gz/sim/Model.hh>
#include <gz/sim/System.hh>
#include <gz/sim/components/Model.hh>
#include <gz/sim/components/Name.hh>

#include "D4dWind.hh"
#include "WindLaw.hh"

using namespace gz;
using namespace sim;

namespace d4d
{
struct DragUav
{
  std::string modelName, linkName;
  std::vector<std::string> rotorNames;
  Entity model{kNullEntity}, link{kNullEntity};
  std::vector<Entity> rotors;
  drone4d::wind::Params p;
  double hoverSumOmega{0.0};
};

class Drone4DWindDrag : public System, public ISystemConfigure, public ISystemPreUpdate
{
public:
  void Configure(const Entity &, const std::shared_ptr<const sdf::Element> &_sdf, EntityComponentManager &,
                 EventManager &) override
  {
    const std::string law = _sdf->Get<std::string>("law", "composite").first;
    if (law != "composite" && law != "linear")
    {
      gzerr << "[Drone4DWindDrag] unknown law '" << law << "', expected composite or linear; wind drag disabled" << std::endl;
      return;
    }
    const double rho = _sdf->Get<double>("rho", 1.225).first;
    const bool motorDrag = _sdf->Get<bool>("motor_rotor_drag", true).first;
    this->slowdown = _sdf->Get<double>("rotor_slowdown", 10.0).first;
    for (auto e = _sdf->FindElement("uav"); e; e = e->GetNextElement("uav"))
    {
      DragUav u;
      u.modelName = e->Get<std::string>("model");
      u.linkName = e->Get<std::string>("link", "base_link").first;
      u.p.law = law == "linear" ? drone4d::wind::Law::kLinear : drone4d::wind::Law::kComposite;
      u.p.rho = rho;
      u.p.motorRotorDrag = motorDrag;
      u.p.mass = e->Get<double>("mass", 0.0).first;
      u.p.cda = e->Get<double>("cda", 0.0).first;
      u.p.cRd = e->Get<double>("c_rd", 0.0).first;
      u.p.k = e->Get<double>("k", 0.0).first;
      u.hoverSumOmega = e->Get<double>("hover_sum_omega", 0.0).first;
      for (auto r = e->FindElement("rotor"); r; r = r->GetNextElement("rotor"))
        u.rotorNames.push_back(r->Get<std::string>("joint"));
      this->uavs.push_back(u);
    }
    this->configured = true;
    gzmsg << "[Drone4DWindDrag] " << this->uavs.size() << " vehicles, law " << law << ", rho " << rho
          << (motorDrag ? ", rotor drag increment" : ", full rotor drag") << std::endl;
  }

  void PreUpdate(const UpdateInfo &_info, EntityComponentManager &_ecm) override
  {
    if (_info.paused || !this->configured) return;
    if (!this->resolved && !this->Resolve(_ecm)) return;
    for (DragUav &u : this->uavs)
    {
      Link link(u.link);
      const auto v = link.WorldLinearVelocity(_ecm);
      const auto pose = link.WorldPose(_ecm);
      if (!v || !pose) continue;  // velocity checks take effect from the next iteration
      const auto *wc = _ecm.Component<components::D4dWind>(u.model);
      const drone4d::WindCmd w = wc != nullptr ? wc->Data() : drone4d::WindCmd{};
      const math::Vector3d b3 = pose->Rot().RotateVector(math::Vector3d::UnitZ);
      double sumOmega = 0.0;
      bool have = !u.rotors.empty();
      for (const Entity &j : u.rotors)
      {
        const auto jv = Joint(j).Velocity(_ecm);
        if (!jv || jv->empty())
        {
          have = false;
          break;
        }
        sumOmega += std::abs((*jv)[0]) * this->slowdown;
      }
      if (!have) sumOmega = u.hoverSumOmega;
      const double vv[3] = {v->X(), v->Y(), v->Z()};
      const double ww[3] = {w.wx, w.wy, w.wz};
      const double bb[3] = {b3.X(), b3.Y(), b3.Z()};
      double f[3];
      drone4d::wind::Force(u.p, vv, ww, bb, sumOmega, f);
      link.AddWorldForce(_ecm, math::Vector3d(f[0], f[1], f[2]));
    }
  }

private:
  bool Resolve(EntityComponentManager &_ecm)
  {
    for (DragUav &u : this->uavs)
    {
      u.model = _ecm.EntityByComponents(components::Model(), components::Name(u.modelName));
      if (u.model == kNullEntity) return false;
      Model m(u.model);
      u.link = m.LinkByName(_ecm, u.linkName);
      if (u.link == kNullEntity) return false;
      Link(u.link).EnableVelocityChecks(_ecm, true);
      u.rotors.clear();
      for (const std::string &n : u.rotorNames)
      {
        const Entity j = m.JointByName(_ecm, n);
        if (j == kNullEntity)
        {
          gzwarn << "[Drone4DWindDrag] " << u.modelName << ": rotor joint " << n << " not found, using hover_sum_omega"
                 << std::endl;
          u.rotors.clear();
          break;
        }
        Joint(j).EnableVelocityCheck(_ecm, true);
        u.rotors.push_back(j);
      }
    }
    this->resolved = true;
    return true;
  }

  std::vector<DragUav> uavs;
  double slowdown{10.0};
  bool configured{false}, resolved{false};
};
}  // namespace d4d

GZ_ADD_PLUGIN(d4d::Drone4DWindDrag, System, d4d::Drone4DWindDrag::ISystemConfigure, d4d::Drone4DWindDrag::ISystemPreUpdate)
GZ_ADD_PLUGIN_ALIAS(d4d::Drone4DWindDrag, "drone4d::Drone4DWindDrag")
