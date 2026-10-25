# ANet4D Python SDK quickstart

<!-- The body of this page is included from docs/sdk/quickstart.en.md in the repository; edit that file. -->

In about 15 minutes you install the `anet4d` SDK, write an example agent, run a 60 s L1 episode with 3 drones on the high-fidelity physics tier (`physics = realistic`, Gazebo Sim lockstep), confirm that the action streams are byte-identical whether the agent connects over the wire protocol or runs inside the Env process, and finally repeat the episode on the fast simulation tier (`physics = fast`) without code changes. Every output shown below comes from a real run; section 0 lists the measurement conditions.

```{include} ../../../docs/sdk/quickstart.en.md
:start-line: 3
```
