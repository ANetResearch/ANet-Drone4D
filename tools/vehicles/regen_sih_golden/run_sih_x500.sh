#!/usr/bin/env bash
# N SIH instances with x500-like physical params injected via PX4_PARAM_* env overrides (no rebuild)
N=${1:-3}
NAME=px4sih_r20
docker rm -f $NAME >/dev/null 2>&1 || true
docker run -d --name $NAME --network host --entrypoint sh \
  -e PX4_SIM_MODEL=sihsim_quadx \
  -e PX4_PARAM_SIH_MASS=2.064 -e PX4_PARAM_SIH_IXX=0.0217 -e PX4_PARAM_SIH_IYY=0.0217 -e PX4_PARAM_SIH_IZZ=0.040 \
  -e PX4_PARAM_SIH_T_MAX=8.55 -e PX4_PARAM_SIH_Q_MAX=0.137 -e PX4_PARAM_SIH_L_ROLL=0.174 -e PX4_PARAM_SIH_L_PITCH=0.174 \
  -e PX4_PARAM_SIH_KDV=0.35 -e PX4_PARAM_SIH_KDW=0.02 -e PX4_PARAM_SIH_T_TAU=0.03 -e PX4_PARAM_MPC_THR_HOVER=0.59 \
  -e PX4_PARAM_CA_ROTOR0_PX=0.174 -e PX4_PARAM_CA_ROTOR0_PY=0.174 -e PX4_PARAM_CA_ROTOR1_PX=-0.174 -e PX4_PARAM_CA_ROTOR1_PY=-0.174 \
  -e PX4_PARAM_CA_ROTOR2_PX=0.174 -e PX4_PARAM_CA_ROTOR2_PY=-0.174 -e PX4_PARAM_CA_ROTOR3_PX=-0.174 -e PX4_PARAM_CA_ROTOR3_PY=0.174 \
  px4io/px4-sitl:v1.18.0-rc1 -c "
    i=0
    while [ \$i -lt $N ]; do
      mkdir -p /tmp/inst_\$i && cd /tmp/inst_\$i
      /opt/px4/bin/px4 -i \$i -d /opt/px4/etc > /tmp/inst_\$i/out.log 2>&1 &
      i=\$((i+1))
    done
    wait" >/dev/null
echo started
