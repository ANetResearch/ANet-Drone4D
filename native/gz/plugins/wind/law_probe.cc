/*
 * Standalone probe of WindLaw.hh (no gz dependency): reads whitespace-separated rows
 *   law(0 linear | 1 composite) motor_rotor_drag(0|1) rho cda c_rd mass k vx vy vz wx wy wz b3x b3y b3z sum_omega
 * from stdin and prints fx fy fz per row with %.17g. Used by tests/d3/phys/test_gz_wind.py to compare the C++ law with
 * the numpy oracle awr.phys.gz.wind.drag_force. Build: g++ -std=c++17 -O2 law_probe.cc -o law_probe. Apache-2.0.
 */
#include <cstdio>

#include "WindLaw.hh"

int main()
{
  int law = 0, motor = 0;
  drone4d::wind::Params p;
  double v[3], w[3], b3[3], so, f[3];
  while (std::scanf("%d %d %lf %lf %lf %lf %lf %lf %lf %lf %lf %lf %lf %lf %lf %lf %lf", &law, &motor, &p.rho, &p.cda,
                    &p.cRd, &p.mass, &p.k, &v[0], &v[1], &v[2], &w[0], &w[1], &w[2], &b3[0], &b3[1], &b3[2], &so) == 17)
  {
    p.law = law == 0 ? drone4d::wind::Law::kLinear : drone4d::wind::Law::kComposite;
    p.motorRotorDrag = motor != 0;
    drone4d::wind::Force(p, v, w, b3, so, f);
    std::printf("%.17g %.17g %.17g\n", f[0], f[1], f[2]);
  }
  return 0;
}
