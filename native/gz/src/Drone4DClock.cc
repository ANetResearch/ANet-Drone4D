/*
 * Drone4DClock (M24 §6.5.2, §6.5.9, §7.4): world-level gz-sim System that gates every iteration on the external
 * authoritative clock (lockstep). The host (awr.phys.gz.backend.GzSimBackend) grants steps over a ZeroMQ REQ/REP socket
 * with drone4d.gz.v1 messages; per granted batch the clock
 *   1. writes due velocity setpoints into the D4dVelocityCmd component of each vehicle model (PreUpdate, before
 *      Drone4DVelocityControl in the same iteration: gz:system_priority -200),
 *   2. sets the mean wind seed (uniform wind mode) when the world has a wind entity,
 *   3. samples the step-end state of every vehicle (PostUpdate): position, attitude, linear and body angular velocity
 *      and body specific force R^T((v_n - v_{n-1})/dt - g), and classifies contacts,
 *   4. replies StepDone with all samples of the batch before blocking for the next Control.
 *
 * SDF parameters: <endpoint> (default ipc:///run/d4d/ctl.sock), <step_ns> (4000000), <lockstep> (true),
 * <recv_poll_ms> (200), repeated <uav model="..." link="base_link"/> in uav_index order.
 *
 * Contacts (thresholds as D2 ContactCfg): normal within 45 degrees of +Z is ground-like; ground contact at a descent
 * speed above 3 m/s is IMPACT, otherwise TOUCHDOWN; steep normals are COLLISION_WORLD; any contact with another vehicle
 * is COLLISION_UAV; leaving a ground contact is LIFTOFF. Landing (stationary 0.5 s or 1 s) is decided by the host.
 *
 * Copyright 2026 Drone4D. Licensed under the Apache License, Version 2.0.
 */
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include <zmq.hpp>

#include <gz/common/Console.hh>
#include <gz/math/Pose3.hh>
#include <gz/math/Vector3.hh>
#include <gz/msgs/contacts.pb.h>
#include <gz/msgs/Utility.hh>
#include <gz/plugin/Register.hh>
#include <gz/sim/Link.hh>
#include <gz/sim/Model.hh>
#include <gz/sim/System.hh>
#include <gz/sim/Util.hh>
#include <gz/sim/components/Collision.hh>
#include <gz/sim/components/ContactSensorData.hh>
#include <gz/sim/components/LinearVelocitySeed.hh>
#include <gz/sim/components/Link.hh>
#include <gz/sim/components/Model.hh>
#include <gz/sim/components/Name.hh>
#include <gz/sim/components/ParentEntity.hh>
#include <gz/sim/components/Wind.hh>
#include <gz/sim/config.hh>

#include "D4dComponents.hh"
#include "drone4d_gz.pb.h"

#ifndef D4D_PLUGIN_BUILD
#define D4D_PLUGIN_BUILD "dev"
#endif

using namespace gz;
using namespace sim;
namespace pb = ::drone4d::gz::v1;

// Not namespace drone4d: the generated protobuf code owns drone4d::gz, which would hide ::gz inside it.
namespace d4d
{
constexpr uint32_t kProtocol = 1;
constexpr int kStateN = 16;
constexpr double kG = 9.80665;
constexpr double kGroundCos = 0.70710678118654757;  // cos 45 degrees
constexpr double kImpactVz = 3.0;

enum ContactKind : uint32_t { kTouchdown = 1, kLiftoff = 2, kWorld = 3, kImpact = 4, kUav = 5 };

struct Uav
{
  std::string modelName, linkName;
  Entity model{kNullEntity}, link{kNullEntity};
  std::set<Entity> collisions;
  std::set<Entity> gear;  // landing-gear collisions (name prefix "gear_")
  math::Vector3d vPrev;
  bool first{true};
  bool ground{false};   // ground-like contact in the previous step
  bool other{false};    // non-ground contact in the previous step
};

class Drone4DClock : public System, public ISystemConfigure, public ISystemPreUpdate, public ISystemPostUpdate
{
public:
  ~Drone4DClock() override
  {
    if (this->sock) this->sock->close();
  }

  void Configure(const Entity &, const std::shared_ptr<const sdf::Element> &_sdf, EntityComponentManager &,
                 EventManager &) override
  {
    this->endpoint = _sdf->Get<std::string>("endpoint", "ipc:///run/d4d/ctl.sock").first;
    this->stepNs = static_cast<int64_t>(_sdf->Get<double>("step_ns", 4000000.0).first);
    this->lockstep = _sdf->Get<bool>("lockstep", true).first;
    this->pollMs = _sdf->Get<int>("recv_poll_ms", 200).first;
    for (auto e = _sdf->FindElement("uav"); e; e = e->GetNextElement("uav"))
    {
      Uav u;
      u.modelName = e->Get<std::string>("model");
      u.linkName = e->Get<std::string>("link", "base_link").first;
      this->uavs.push_back(u);
    }
    this->sock = std::make_unique<zmq::socket_t>(this->ctx, zmq::socket_type::rep);
    this->sock->set(zmq::sockopt::rcvtimeo, this->pollMs);
    this->sock->set(zmq::sockopt::linger, 0);
    this->sock->bind(this->endpoint);
    gzmsg << "[Drone4DClock] " << this->uavs.size() << " vehicles, step " << this->stepNs << " ns, "
          << (this->lockstep ? "lockstep" : "free running") << ", endpoint " << this->endpoint << std::endl;
  }

  void PreUpdate(const UpdateInfo &_info, EntityComponentManager &_ecm) override
  {
    if (_info.paused || this->shutdown) return;
    if (!this->resolved && !this->Resolve(_ecm)) return;
    if (this->lockstep && this->budget == 0)
    {
      if (this->pending)
      {
        this->SendStepDone(_info);
        this->pending = false;
      }
      if (!this->Serve(_info, _ecm)) return;
    }
    if (this->lockstep) --this->budget;
    this->ApplyDue(_ecm);
    this->ApplyWind(_ecm);
    ++this->stepInBatch;
  }

  void PostUpdate(const UpdateInfo &_info, const EntityComponentManager &_ecm) override
  {
    if (_info.paused || this->shutdown || !this->resolved || !this->granted) return;
    const int64_t t = std::chrono::duration_cast<std::chrono::nanoseconds>(_info.simTime).count();
    const double dt = this->stepNs * 1e-9;
    this->tNs.push_back(t);
    const uint32_t step = this->stepInBatch - 1;
    for (size_t i = 0; i < this->uavs.size(); ++i)
    {
      Uav &u = this->uavs[i];
      Link link(u.link);
      const math::Pose3d P = link.WorldPose(_ecm).value_or(math::Pose3d::Zero);
      const math::Vector3d v = link.WorldLinearVelocity(_ecm).value_or(math::Vector3d::Zero);
      const math::Vector3d wW = link.WorldAngularVelocity(_ecm).value_or(math::Vector3d::Zero);
      const math::Vector3d a = u.first ? math::Vector3d::Zero : (v - u.vPrev) / dt;
      const math::Vector3d wB = P.Rot().RotateVectorReverse(wW);
      const math::Vector3d fB = P.Rot().RotateVectorReverse(a - math::Vector3d(0, 0, -kG));
      const double row[kStateN] = {P.Pos().X(), P.Pos().Y(), P.Pos().Z(), P.Rot().X(), P.Rot().Y(), P.Rot().Z(),
                                   P.Rot().W(), v.X(),       v.Y(),       v.Z(),       wB.X(),      wB.Y(),
                                   wB.Z(),      fB.X(),      fB.Y(),      fB.Z()};
      this->samples.insert(this->samples.end(), row, row + kStateN);
      const math::Vector3d vPre = u.first ? v : u.vPrev;  // contact resolution has already stopped v: judge impacts by the step before
      u.vPrev = v;
      u.first = false;
      this->Classify(i, step, vPre, _ecm);
    }
    this->pending = true;
  }

private:
  bool Resolve(EntityComponentManager &_ecm)
  {
    for (auto &u : this->uavs)
    {
      u.model = _ecm.EntityByComponents(components::Model(), components::Name(u.modelName));
      if (u.model == kNullEntity) return false;
      Model m(u.model);
      u.link = m.LinkByName(_ecm, u.linkName);
      if (u.link == kNullEntity) return false;
      Link(u.link).EnableVelocityChecks(_ecm, true);
      for (const Entity &l : _ecm.EntitiesByComponents(components::ParentEntity(u.model), components::Link()))
      {
        for (const Entity &c : _ecm.ChildrenByComponents(l, components::Collision()))
        {
          if (!_ecm.Component<components::ContactSensorData>(c))
            _ecm.CreateComponent(c, components::ContactSensorData());
          u.collisions.insert(c);
          const auto *nm = _ecm.Component<components::Name>(c);
          if (nm != nullptr && nm->Data().rfind("gear_", 0) == 0) u.gear.insert(c);
          this->collisionOwner[c] = static_cast<size_t>(&u - &this->uavs[0]);
        }
      }
    }
    this->resolved = true;
    return true;
  }

  /// Block on the REP socket until a Control grants steps (true) or the server shuts down (false).
  bool Serve(const UpdateInfo &_info, const EntityComponentManager &_ecm)
  {
    for (;;)
    {
      zmq::message_t req;
      try
      {
        if (!this->sock->recv(req, zmq::recv_flags::none)) continue;  // poll timeout
      }
      catch (const zmq::error_t &e)  // EINTR on SIGTERM, ETERM: end serving, let the server exit
      {
        gzmsg << "[Drone4DClock] control socket closed: " << e.what() << std::endl;
        this->shutdown = true;
        return false;
      }
      pb::Envelope in;
      if (!in.ParseFromArray(req.data(), static_cast<int>(req.size())) || in.protocol() != kProtocol)
      {
        this->SendError(635, "unparseable envelope or protocol mismatch");
        continue;
      }
      if (in.has_hello())
      {
        this->SendHelloAck(_info, _ecm, in.hello());
        continue;
      }
      if (in.has_shutdown())
      {
        pb::Envelope out;
        out.set_protocol(kProtocol);
        out.mutable_bye()->set_iterations(this->iterations);
        this->Send(out);
        this->shutdown = true;
        return false;
      }
      if (!in.has_control())
      {
        this->SendError(635, "expected Control");
        continue;
      }
      const pb::Control &c = in.control();
      if (c.seq() == this->seq && !this->cached.empty())
      {
        zmq::message_t rep(this->cached.data(), this->cached.size());
        this->sock->send(rep, zmq::send_flags::none);
        continue;
      }
      std::string why;
      if (c.seq() != this->seq + 1) why = "seq " + std::to_string(c.seq()) + " after " + std::to_string(this->seq);
      else if (c.grant_steps() < 1) why = "grant_steps < 1";
      for (const auto &sp : c.setpoints())
        if (sp.uav_index() >= this->uavs.size() || sp.apply_step() >= c.grant_steps()) why = "setpoint index out of range";
      if (!why.empty())
      {
        this->SendError(635, why);
        continue;
      }
      this->due.assign(c.setpoints().begin(), c.setpoints().end());
      std::stable_sort(this->due.begin(), this->due.end(),
                       [](const pb::VelSp &a, const pb::VelSp &b) { return a.apply_step() < b.apply_step(); });
      this->dueNext = 0;
      this->meanWind = math::Vector3d(c.mean_wind_x(), c.mean_wind_y(), c.mean_wind_z());
      this->budget = c.grant_steps();
      this->stepInBatch = 0;
      this->samples.clear();
      this->tNs.clear();
      this->contacts.clear();
      this->seq = c.seq();
      this->tick = c.tick();
      this->granted = true;
      this->wall0 = std::chrono::steady_clock::now();
      this->sim0 = _info.simTime;
      return true;
    }
  }

  void ApplyDue(EntityComponentManager &_ecm)
  {
    while (this->dueNext < this->due.size() && this->due[this->dueNext].apply_step() == this->stepInBatch)
    {
      const pb::VelSp &sp = this->due[this->dueNext++];
      ::drone4d::VelocityCmd cmd{sp.vx(), sp.vy(), sp.vz(), sp.yaw_rate(), sp.enabled()};
      const Entity m = this->uavs[sp.uav_index()].model;
      auto *comp = _ecm.Component<components::D4dVelocityCmd>(m);
      if (comp == nullptr) _ecm.CreateComponent(m, components::D4dVelocityCmd(cmd));
      else if (comp->Data() != cmd)
      {
        comp->Data() = cmd;
        _ecm.SetChanged(m, components::D4dVelocityCmd::typeId, ComponentState::OneTimeChange);
      }
    }
  }

  void ApplyWind(EntityComponentManager &_ecm)
  {
    if (this->stepInBatch != 0) return;
    const Entity w = _ecm.EntityByComponents(components::Wind());
    if (w == kNullEntity) return;
    auto *seed = _ecm.Component<components::WorldLinearVelocitySeed>(w);
    if (seed == nullptr) _ecm.CreateComponent(w, components::WorldLinearVelocitySeed(this->meanWind));
    else if (seed->Data() != this->meanWind)
    {
      seed->Data() = this->meanWind;
      _ecm.SetChanged(w, components::WorldLinearVelocitySeed::typeId, ComponentState::OneTimeChange);
    }
  }

  void Classify(size_t _i, uint32_t _step, const math::Vector3d &_v, const EntityComponentManager &_ecm)
  {
    Uav &u = this->uavs[_i];
    bool ground = false, other = false, uavHit = false;
    math::Vector3d posG, nrmG, posO, nrmO;
    for (const Entity &c : u.collisions)
    {
      const auto *data = _ecm.Component<components::ContactSensorData>(c);
      if (data == nullptr) continue;
      for (const auto &k : data->Data().contact())
      {
        const Entity e1 = k.collision1().id(), e2 = k.collision2().id();
        const bool firstIsOwn = u.collisions.count(e1) > 0;
        const Entity mine = firstIsOwn ? e1 : e2, peer = firstIsOwn ? e2 : e1;
        auto own = this->collisionOwner.find(peer);
        if (own != this->collisionOwner.end() && own->second == _i) continue;  // self contact
        const math::Vector3d p = k.position_size() > 0 ? msgs::Convert(k.position(0)) : math::Vector3d::Zero;
        const math::Vector3d n = k.normal_size() > 0 ? msgs::Convert(k.normal(0)) : math::Vector3d::Zero;
        // Ground: normal within 45 degrees of vertical. Landing gear on uneven mesh terrain (triangle edges) reports lateral
        // normals: gear contacts always count as ground, only body and rotor contacts with lateral normals are collisions.
        if (own != this->collisionOwner.end()) { uavHit = true; posO = p; nrmO = n; }
        else if (std::abs(n.Z()) >= kGroundCos * n.Length() || u.gear.count(mine)) { ground = true; posG = p; nrmG = n; }
        else { other = true; posO = p; nrmO = n; }
      }
    }
    auto emit = [&](uint32_t kind, const math::Vector3d &pos, const math::Vector3d &nrm) {
      pb::Contact ct;
      ct.set_uav_index(static_cast<uint32_t>(_i));
      ct.set_step(_step);
      ct.set_kind(kind);
      ct.set_px(pos.X()); ct.set_py(pos.Y()); ct.set_pz(pos.Z());
      ct.set_nx(nrm.X()); ct.set_ny(nrm.Y()); ct.set_nz(nrm.Z());
      ct.set_speed(_v.Length());
      this->contacts.push_back(ct);
    };
    if (uavHit) emit(kUav, posO, nrmO);
    if (ground && !u.ground) emit(-_v.Z() > kImpactVz ? kImpact : kTouchdown, posG, nrmG);
    if (!ground && u.ground) emit(kLiftoff, posG, nrmG);
    if (other && !u.other) emit(kWorld, posO, nrmO);
    u.ground = ground;
    u.other = other;
  }

  void Send(const pb::Envelope &_e)
  {
    std::string buf;
    _e.SerializeToString(&buf);
    zmq::message_t rep(buf.data(), buf.size());
    this->sock->send(rep, zmq::send_flags::none);
  }

  void SendError(uint32_t _code, const std::string &_detail)
  {
    gzerr << "[Drone4DClock] protocol error " << _code << ": " << _detail << std::endl;
    pb::Envelope out;
    out.set_protocol(kProtocol);
    out.mutable_error()->set_code(_code);
    out.mutable_error()->set_detail(_detail);
    this->Send(out);
  }

  void SendHelloAck(const UpdateInfo &_info, const EntityComponentManager &_ecm, const pb::Hello &_h)
  {
    pb::Envelope out;
    out.set_protocol(kProtocol);
    if (static_cast<size_t>(_h.uav_models_size()) != this->uavs.size() ||
        (_h.step_ns() != 0 && static_cast<int64_t>(_h.step_ns()) != this->stepNs))
    {
      this->SendError(635, "Hello does not match the composed world (vehicles or step)");
      return;
    }
    for (int i = 0; i < _h.uav_models_size(); ++i)
      if (_h.uav_models(i) != this->uavs[i].modelName)
      {
        this->SendError(635, "uav_models[" + std::to_string(i) + "] = " + _h.uav_models(i));
        return;
      }
    auto *ack = out.mutable_hello_ack();
    ack->set_gz_sim_version(GZ_SIM_VERSION_FULL);
    ack->set_plugin_build(D4D_PLUGIN_BUILD);
    ack->set_n_uav(static_cast<uint32_t>(this->uavs.size()));
    // Served in PreUpdate: simTime is already the end of this iteration's step, the reported state is one step earlier.
    ack->set_t_ns(std::chrono::duration_cast<std::chrono::nanoseconds>(_info.simTime - _info.dt).count());
    std::vector<double> st;
    for (const auto &u : this->uavs)
    {
      const math::Pose3d P = Link(u.link).WorldPose(_ecm).value_or(math::Pose3d::Zero);
      const double row[kStateN] = {P.Pos().X(), P.Pos().Y(), P.Pos().Z(), P.Rot().X(), P.Rot().Y(), P.Rot().Z(), P.Rot().W(),
                                   0, 0, 0, 0, 0, 0, 0, 0, 0};
      math::Vector3d fB = P.Rot().RotateVectorReverse(math::Vector3d(0, 0, kG));
      std::vector<double> r(row, row + kStateN);
      r[13] = fB.X(); r[14] = fB.Y(); r[15] = fB.Z();
      st.insert(st.end(), r.begin(), r.end());
    }
    ack->set_initial_state(st.data(), st.size() * sizeof(double));
    this->Send(out);
  }

  void SendStepDone(const UpdateInfo &_info)
  {
    pb::Envelope out;
    out.set_protocol(kProtocol);
    auto *sd = out.mutable_step_done();
    sd->set_seq(this->seq);
    sd->set_tick(this->tick);
    sd->set_n_steps(static_cast<uint32_t>(this->tNs.size()));
    sd->set_n_uav(static_cast<uint32_t>(this->uavs.size()));
    for (int64_t t : this->tNs) sd->add_t_ns(t);
    sd->set_samples(this->samples.data(), this->samples.size() * sizeof(double));
    for (const auto &c : this->contacts) *sd->add_contacts() = c;
    const double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - this->wall0).count();
    const double sim = std::chrono::duration<double>(_info.simTime - this->sim0).count();
    sd->set_gz_rtf(wall > 0 ? static_cast<float>(sim / wall) : 0.0f);
    this->iterations += this->tNs.size();
    out.SerializeToString(&this->cached);
    zmq::message_t rep(this->cached.data(), this->cached.size());
    this->sock->send(rep, zmq::send_flags::none);
  }

  std::string endpoint;
  int64_t stepNs{4000000};
  bool lockstep{true};
  int pollMs{200};
  std::vector<Uav> uavs;
  std::map<Entity, size_t> collisionOwner;
  zmq::context_t ctx{1};
  std::unique_ptr<zmq::socket_t> sock;
  bool resolved{false}, granted{false}, pending{false}, shutdown{false};
  uint64_t seq{0}, tick{0}, iterations{0};
  uint32_t budget{0}, stepInBatch{0};
  std::vector<pb::VelSp> due;
  size_t dueNext{0};
  math::Vector3d meanWind;
  std::vector<double> samples;
  std::vector<int64_t> tNs;
  std::vector<pb::Contact> contacts;
  std::string cached;
  std::chrono::steady_clock::time_point wall0;
  std::chrono::steady_clock::duration sim0{};
};
}  // namespace d4d

GZ_ADD_PLUGIN(d4d::Drone4DClock, System, d4d::Drone4DClock::ISystemConfigure, d4d::Drone4DClock::ISystemPreUpdate,
              d4d::Drone4DClock::ISystemPostUpdate)
GZ_ADD_PLUGIN_ALIAS(d4d::Drone4DClock, "drone4d::Drone4DClock")
