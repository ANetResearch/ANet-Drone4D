# Citation and license

## Citing ANet Drone4D

GitHub's "Cite this repository" reads [CITATION.cff](https://github.com/ANetResearch/ANet-Drone4D/blob/main/CITATION.cff) at the repository
root. BibTeX:

```bibtex
@software{anet_drone4d_2026,
  title   = {ANet-Drone4D: A Real-World Grounded 4D World Runtime for Autonomous Multi-Agent Drones},
  author  = {{Agent Network Research}},
  year    = {2026},
  version = {0.2.0},
  url     = {https://github.com/ANetResearch/ANet-Drone4D}
}
```

## Citing UrbanScene3D

Works that use the six UrbanScene3D cities (Shenzhen, Shanghai, New York, San Francisco, Suzhou, Chicago) should cite the original paper:

```bibtex
@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}
```

## ANet research

The agent part builds on ANet's research on the value of connection in agent networks:

```bibtex
@article{yuan2026patu1,
  title   = {ANet Patu-1: The Value of Connection in the Agent Network},
  author  = {Yuan, Mu and Song, Jinke and Zhou, Zhaomeng and Zhang, Lan},
  journal = {arXiv preprint arXiv:2607.15053},
  year    = {2026}
}

@article{zhang2026agentnetwork,
  title   = {Agent Network for Open Multi-Agent Collaboration with Shared Cognition},
  author  = {Zhang, Lan and Liu, Yunhao},
  journal = {Tsinghua Science and Technology},
  volume  = {31},
  number  = {6},
  pages   = {2611--2629},
  year    = {2026},
  doi     = {10.26599/TST.2026.9010062}
}
```

## License

ANet Drone4D is released under the **ANet Open Source License (ANet-Drone4D)**, a modified Apache License 2.0:

- **Commercial use is allowed.** The World Runtime, the World Package tools and the web sandbox can be embedded in your products or run on
  your own infrastructure.
- **Two additional conditions.** Operating a multi-tenant hosted simulation or digital twin service for unrelated organizations or
  individuals requires written permission (services for personal use or within one organization, free research or teaching services run by or
  for universities, schools, public or non-profit research institutions, and letting others watch an instance you run yourself need no
  permission); the ANet logo and copyright notices in the web sandbox and the `awr` CLI must be kept.
- **Data and third-party parts follow their own terms.** UrbanScene3D is for non-commercial use only and may not be redistributed (this
  project's own deployments use it under authorization of the dataset rights holders, see {doc}`../topics/worlds`); the included and adapted
  third-party components and their licenses are listed in
  [THIRD_PARTY_NOTICES.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/THIRD_PARTY_NOTICES.md).

The full terms are in [LICENSE](https://github.com/ANetResearch/ANet-Drone4D/blob/main/LICENSE) and
[NOTICE](https://github.com/ANetResearch/ANet-Drone4D/blob/main/NOTICE). For commercial licensing and related questions, contact
hi@anet0.com.

## Marks

The Agent Network logo at the top left of this site and in the web sandbox, and the Agent Network Research avatar, are marks of Agent Network
Research; they stay in the user interfaces as license condition 1b requires, and no trademark license is granted.

## Acknowledgments

ANet Drone4D builds on open work: the viewport uses [three.js](https://threejs.org) and React Three Fiber, the octree container layout comes
from Potree, the bus is [zenoh](https://zenoh.io), the fleet kernel uses [numba](https://numba.pydata.org), the control cascade and SIH
reference flights come from PX4, the P600 airframe comes from Prometheus, and the cities come from UrbanScene3D. This documentation site is
built with [Sphinx](https://www.sphinx-doc.org/en/master/) and the [Read the Docs theme](https://sphinx-rtd-theme.readthedocs.io/en/stable/).
