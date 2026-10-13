# 引用与许可

## 引用 ANet Drone4D

GitHub 的"Cite this repository"读取仓库根目录的 [CITATION.cff](https://github.com/ANetResearch/ANet-Drone4D/blob/main/CITATION.cff)。BibTeX 如下：

```bibtex
@software{anet_drone4d_2026,
  title   = {ANet-Drone4D: A Real-World Grounded 4D World Runtime for Autonomous Multi-Agent Drones},
  author  = {{Agent Network Research}},
  year    = {2026},
  version = {0.2.0},
  url     = {https://github.com/ANetResearch/ANet-Drone4D}
}
```

## 引用 UrbanScene3D

使用六座 UrbanScene3D 城市（深圳、上海、纽约、旧金山、苏州、芝加哥）的工作，请引用原论文：

```bibtex
@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}
```

## ANet 研究

智能体部分建立在 ANet 关于"智能体网络中连接的价值"的研究之上：

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

## 许可证

ANet Drone4D 以 **ANet 开源许可证（ANet-Drone4D）** 发布，它是改版的 Apache License 2.0：

- **允许商用。** World Runtime、World Package 工具与 Web 沙盘可以嵌入您的产品，或在自有基础设施上运行。
- **两项附加条件。** 运营面向互不相关的组织或个人的多租户托管仿真或数字孪生服务需要书面授权（仅供个人或仅在一个组织内部使用的服务、由大学、学校、
  公共或非营利科研机构运营或为其运营的免费科研或教学服务、让他人观看自行运行的实例，均不需要授权）；Web 沙盘与 `awr` CLI 中的 ANet 标志和版权信息须保留。
- **数据与第三方部分适用各自条款。** UrbanScene3D 仅限非商业使用且不得再分发（本项目自己的部署经数据集权利方授权使用，见 {doc}`../topics/worlds`）；
  被包含和改写的第三方组件及其许可列在
  [THIRD_PARTY_NOTICES.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/THIRD_PARTY_NOTICES.md) 中。

完整条款见 [LICENSE](https://github.com/ANetResearch/ANet-Drone4D/blob/main/LICENSE) 与
[NOTICE](https://github.com/ANetResearch/ANet-Drone4D/blob/main/NOTICE)。商业授权与相关问题请联系 hi@anet0.com。

## 标志

本文档站左上角与 Web 沙盘中的 Agent Network 标志及 Agent Network Research 头像是 Agent Network Research 的标识，按许可证第 1b 条在用户界面中保留，
不授予商标许可。

## 致谢

ANet Drone4D 建立在开放的工作之上：视口使用 [three.js](https://threejs.org) 与 React Three Fiber，八叉树容器布局来自 Potree，总线使用
[zenoh](https://zenoh.io)，机群内核使用 [numba](https://numba.pydata.org)，控制级联与 SIH 参考飞行来自 PX4，P600 机体来自 Prometheus，城市来自
UrbanScene3D。本文档站使用 [Sphinx](https://www.sphinx-doc.org/en/master/) 与 [Read the Docs 主题](https://sphinx-rtd-theme.readthedocs.io/en/stable/) 构建。
