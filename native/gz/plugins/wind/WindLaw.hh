/*
 * Drag laws of the field wind mode (M24 §6.5.8, M24-FR-046), shared by Drone4DWindDrag and the standalone probe
 * (law_probe.cc, tests/d3/phys/test_gz_wind.py). Plain C++17 without gz dependencies so the law can be compiled and
 * compared against the numpy oracle awr.phys.gz.wind.drag_force on any host.
 *
 * All vectors are world ENU. v: link velocity, w: wind (ENU, the direction the air moves to), b3: body z axis,
 * sumOmega: sum of the real rotor speeds in rad/s.
 *   linear:    F = m k (w - v)                                     (WindEffects form, k in 1/s)
 *   composite: F = -1/2 rho CdA |v_r| v_r - sumOmega c_rd (x - (x . b3) b3),  v_r = v - w
 *              with x = v_r (full rotor drag) or x = -w when the motor model already applies -sumOmega c_rd v_perp on the
 *              ground velocity (gz MulticopterMotorModel rotorDragCoefficient, no wind entity in the field mode), so that
 *              the total equals the FleetSim `aero = composite` law on the relative airspeed (M08 §6.5.4).
 * The operation order follows awr.sim.fleet.aero.aero_force term by term.
 *
 * Copyright 2026 Drone4D. Licensed under the Apache License, Version 2.0.
 */
#ifndef D4D_WIND_LAW_HH_
#define D4D_WIND_LAW_HH_

#include <cmath>

namespace drone4d
{
namespace wind
{
enum class Law : int { kLinear = 0, kComposite = 1 };

struct Params
{
  Law law{Law::kComposite};
  double rho{1.225};      // kg/m^3
  double cda{0.0};        // m^2
  double cRd{0.0};        // N s / (rad m)
  double mass{0.0};       // kg (linear law)
  double k{0.0};          // 1/s (linear law)
  bool motorRotorDrag{true};
};

inline void Force(const Params &_p, const double _v[3], const double _w[3], const double _b3[3], double _sumOmega,
                  double _out[3])
{
  const double vr[3] = {_v[0] - _w[0], _v[1] - _w[1], _v[2] - _w[2]};
  if (_p.law == Law::kLinear)
  {
    const double km = _p.mass * _p.k;
    for (int c = 0; c < 3; ++c) _out[c] = -km * vr[c];
    return;
  }
  const double spd = std::sqrt(vr[0] * vr[0] + vr[1] * vr[1] + vr[2] * vr[2]);
  const double x[3] = {_p.motorRotorDrag ? -_w[0] : vr[0], _p.motorRotorDrag ? -_w[1] : vr[1],
                       _p.motorRotorDrag ? -_w[2] : vr[2]};
  const double dotb = x[0] * _b3[0] + x[1] * _b3[1] + x[2] * _b3[2];
  const double kq = 0.5 * _p.rho * _p.cda;
  const double kr = _sumOmega * _p.cRd;
  for (int c = 0; c < 3; ++c) _out[c] = -kq * spd * vr[c] - kr * (x[c] - dotb * _b3[c]);
}
}  // namespace wind
}  // namespace drone4d

#endif
