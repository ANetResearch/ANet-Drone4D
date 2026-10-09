#!/bin/bash
while [ ! -f /tmp/chain_tail.done ]; do sleep 30; done
cd /root/anet-drone
PYTHONPATH=python:tools/paper .venv/bin/python tools/paper/sweep.py rtl --procs 60 --run-id front seeds=5 presets=clear front=rain,heavyRain,thunderstorm policies=px4,coupled,coupled_4d > /tmp/sweep_front.log 2>&1
touch /tmp/chain_front.done
