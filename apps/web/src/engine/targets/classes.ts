// Target class templates (M18-FR-002; AWR-04 §7.2; contract packages/contracts/target/class_defaults.json, version 1.0.0).
// Owner: M18. A literal copy of the contract file (the web bundle cannot import JSON outside apps/web); the vitest
// apps/web/tests/m18/classes.test.ts fails when it drifts from the contract. Values are simulation reference values
// (UI shows "模拟参考值" next to them). Regenerate: read the JSON and paste `classes` and `label_zh`.

export type TargetClassName = 'person' | 'uav' | 'machine' | 'dog' | 'vehicle' | 'object'
export const TARGET_CLASSES: readonly TargetClassName[] = ['person', 'uav', 'machine', 'dog', 'vehicle', 'object']
/** TargetClass wire codes (rt/enums.json TargetClass: 0 UNKNOWN, 1 PERSON, ...) */
export const CLASS_CODE: Readonly<Record<TargetClassName, number>> = { person: 1, uav: 2, machine: 3, dog: 4, vehicle: 5, object: 6 }
export const CLASS_OF_CODE: readonly (TargetClassName | null)[] = [null, 'person', 'uav', 'machine', 'dog', 'vehicle', 'object']
export const CLASS_DEFAULTS_VERSION = '1.0.0'
export const LABEL_ZH: Readonly<Record<TargetClassName, string>> = {person: "人", uav: "机", machine: "机器", dog: "狗", vehicle: "车", object: "物"}

export const CLASS_DEFAULTS = {
  person: {
    class: "person",
    enabled: true,
    size_m: {
      l: 0.5,
      w: 0.3,
      h: 1.75
    },
    critical_dim_m: null,
    motion: {
      mode: "random_walk",
      speed_mps: 1.4,
      speed_max_mps: 2.5,
      accel_mps2: 1.5,
      turn_rate_max_rad_s: 1.0,
      speed_sigma_mps: 0.2,
      turn_tau_s: 4.0,
      pause_prob: 0.02
    },
    albedo_vis: 0.25,
    contrast0: 0.4,
    camouflage: 0.0,
    occlusion: 0.0,
    reflect_850: 0.45,
    reflect_905: 0.4,
    thermal: {
      mode: "relative",
      delta_t_k: 8,
      emissivity: 0.97,
      hot_fraction: 1.0
    },
    rcs_m2: 0.5,
    rcs_fluct: "swerling0",
    rcs_band: "77ghz",
    sound: {
      waveform: "pulse_train",
      f0_hz: 200,
      harmonics_db: [
        0
      ],
      lw_dba: 70,
      period_s: 8,
      duty: 0.25,
      phase_s: 0
    },
    rf: null,
    criterion_mode: "geometric",
    alertness: 0.5,
    hearing_th_dba: 20,
    acoustic_adv_db: 0,
    visual_theta0_arcmin: 1.0,
    level_req: "R",
    n_req_px: null,
    reaction: {
      onset: {
        kind: "look_at",
        duration_s: 10
      },
      response: [
        {
          kind: "flee",
          speed_mps: 2.5,
          dist_m: 100
        }
      ],
      alert_others: true
    },
    calm_s: 60,
    alert_radius_m: 50,
    sensitivity: [
      {
        kind: "visual",
        shape: "sector",
        radius_max_m: 300,
        z_min_m: 0.0,
        z_max_m: 300.0,
        half_angle_deg: 60,
        azimuth_offset_deg: 0,
        gain_back_db: 0,
        enabled: true
      },
      {
        kind: "acoustic",
        shape: "hemisphere",
        radius_max_m: 300,
        z_min_m: 0,
        z_max_m: 300,
        half_angle_deg: 180,
        azimuth_offset_deg: 0,
        gain_back_db: 0.0,
        enabled: true
      }
    ]
  },
  uav: {
    class: "uav",
    enabled: true,
    size_m: {
      l: 0.35,
      w: 0.35,
      h: 0.1
    },
    critical_dim_m: null,
    motion: {
      mode: "static",
      speed_mps: 5.0,
      speed_max_mps: 8.0,
      accel_mps2: 1.0,
      turn_rate_max_rad_s: 1.5
    },
    albedo_vis: 0.1,
    contrast0: -0.6,
    camouflage: 0.0,
    occlusion: 0.0,
    reflect_850: 0.1,
    reflect_905: 0.1,
    thermal: {
      mode: "relative",
      delta_t_k: 25,
      emissivity: 0.9,
      hot_fraction: 0.2
    },
    rcs_m2: 0.01,
    rcs_fluct: "swerling1",
    rcs_band: "77ghz",
    sound: {
      waveform: "harmonic",
      f0_hz: 220,
      harmonics_db: [
        0,
        -6,
        -10,
        -14
      ],
      lw_dba: 83,
      period_s: 1,
      duty: 1.0,
      phase_s: 0
    },
    rf: null,
    criterion_mode: "geometric",
    alertness: 1.0,
    hearing_th_dba: 20,
    acoustic_adv_db: 0,
    visual_theta0_arcmin: 1.0,
    level_req: "R",
    n_req_px: null,
    reaction: {
      onset: {
        kind: "none",
        duration_s: 0
      },
      response: [
        {
          kind: "return",
          speed_mps: 8
        }
      ],
      alert_others: false
    },
    calm_s: 60,
    alert_radius_m: 50,
    sensitivity: [
      {
        kind: "visual",
        shape: "sector",
        radius_max_m: 800,
        z_min_m: -50,
        z_max_m: 300,
        half_angle_deg: 40,
        azimuth_offset_deg: 0,
        gain_back_db: 0,
        enabled: true
      }
    ],
    alt_agl_m: 50
  },
  machine: {
    class: "machine",
    enabled: true,
    size_m: {
      l: 1.0,
      w: 0.7,
      h: 0.8
    },
    critical_dim_m: null,
    motion: {
      mode: "static",
      speed_mps: 0.0,
      speed_max_mps: 0.0,
      accel_mps2: 1.0,
      turn_rate_max_rad_s: 0.5
    },
    albedo_vis: 0.3,
    contrast0: 0.3,
    camouflage: 0.0,
    occlusion: 0.0,
    reflect_850: 0.3,
    reflect_905: 0.3,
    thermal: {
      mode: "relative",
      delta_t_k: 40,
      emissivity: 0.9,
      hot_fraction: 0.3
    },
    rcs_m2: 2.0,
    rcs_fluct: "swerling0",
    rcs_band: "77ghz",
    sound: {
      waveform: "harmonic",
      f0_hz: 100,
      harmonics_db: [
        0,
        -6,
        -10,
        -14
      ],
      lw_dba: 95,
      period_s: 1,
      duty: 1.0,
      phase_s: 0
    },
    rf: null,
    criterion_mode: "geometric",
    alertness: 0.0,
    hearing_th_dba: 20,
    acoustic_adv_db: 0,
    visual_theta0_arcmin: 1.0,
    level_req: "R",
    n_req_px: null,
    reaction: {
      onset: {
        kind: "none",
        duration_s: 0
      },
      response: [
        {
          kind: "ignore"
        }
      ],
      alert_others: false
    },
    calm_s: 60,
    alert_radius_m: 50,
    sensitivity: []
  },
  dog: {
    class: "dog",
    enabled: true,
    size_m: {
      l: 0.9,
      w: 0.3,
      h: 0.6
    },
    critical_dim_m: null,
    motion: {
      mode: "random_walk",
      speed_mps: 1.5,
      speed_max_mps: 8.0,
      accel_mps2: 1.5,
      turn_rate_max_rad_s: 2.0,
      speed_sigma_mps: 0.3,
      turn_tau_s: 4.0,
      pause_prob: 0.02
    },
    albedo_vis: 0.2,
    contrast0: 0.3,
    camouflage: 0.0,
    occlusion: 0.0,
    reflect_850: 0.35,
    reflect_905: 0.3,
    thermal: {
      mode: "relative",
      delta_t_k: 6,
      emissivity: 0.95,
      hot_fraction: 1.0
    },
    rcs_m2: 0.1,
    rcs_fluct: "swerling0",
    rcs_band: "77ghz",
    sound: {
      waveform: "pulse_train",
      f0_hz: 600,
      harmonics_db: [
        0
      ],
      lw_dba: 100,
      period_s: 6,
      duty: 0.15,
      phase_s: 0
    },
    rf: null,
    criterion_mode: "geometric",
    alertness: 0.7,
    hearing_th_dba: 15,
    acoustic_adv_db: 5,
    visual_theta0_arcmin: 1.0,
    level_req: "R",
    n_req_px: null,
    reaction: {
      onset: {
        kind: "none",
        duration_s: 0
      },
      response: [
        {
          kind: "emit",
          sound: {
            duty: 0.6
          },
          duration_s: 30
        },
        {
          kind: "approach",
          dist_m: 20
        }
      ],
      alert_others: true
    },
    calm_s: 60,
    alert_radius_m: 50,
    sensitivity: [
      {
        kind: "visual",
        shape: "sector",
        radius_max_m: 200,
        z_min_m: 0.0,
        z_max_m: 300.0,
        half_angle_deg: 90,
        azimuth_offset_deg: 0,
        gain_back_db: 0,
        enabled: true
      },
      {
        kind: "acoustic",
        shape: "hemisphere",
        radius_max_m: 250,
        z_min_m: 0,
        z_max_m: 250,
        half_angle_deg: 180,
        azimuth_offset_deg: 0,
        gain_back_db: 0.0,
        enabled: true
      }
    ]
  },
  vehicle: {
    class: "vehicle",
    enabled: true,
    size_m: {
      l: 4.5,
      w: 1.8,
      h: 1.5
    },
    critical_dim_m: null,
    motion: {
      mode: "static",
      speed_mps: 10.0,
      speed_max_mps: 15.0,
      accel_mps2: 1.0,
      turn_rate_max_rad_s: 0.5
    },
    albedo_vis: 0.4,
    contrast0: 0.5,
    camouflage: 0.0,
    occlusion: 0.0,
    reflect_850: 0.5,
    reflect_905: 0.5,
    thermal: {
      mode: "relative",
      delta_t_k: 50,
      emissivity: 0.9,
      hot_fraction: 0.2
    },
    rcs_m2: 10.0,
    rcs_fluct: "swerling0",
    rcs_band: "77ghz",
    sound: {
      waveform: "harmonic",
      f0_hz: 30,
      harmonics_db: [
        0,
        -6,
        -10,
        -14
      ],
      lw_dba: 95,
      period_s: 1,
      duty: 1.0,
      phase_s: 0
    },
    rf: null,
    criterion_mode: "geometric",
    alertness: 0.3,
    hearing_th_dba: 20,
    acoustic_adv_db: 0,
    visual_theta0_arcmin: 1.0,
    level_req: "R",
    n_req_px: null,
    reaction: {
      onset: {
        kind: "none",
        duration_s: 0
      },
      response: [
        {
          kind: "ignore"
        }
      ],
      alert_others: false
    },
    calm_s: 60,
    alert_radius_m: 50,
    sensitivity: [
      {
        kind: "visual",
        shape: "sector",
        radius_max_m: 150,
        z_min_m: 0.0,
        z_max_m: 300.0,
        half_angle_deg: 60,
        azimuth_offset_deg: 0,
        gain_back_db: 0,
        enabled: true
      },
      {
        kind: "acoustic",
        shape: "hemisphere",
        radius_max_m: 100,
        z_min_m: 0,
        z_max_m: 100,
        half_angle_deg: 180,
        azimuth_offset_deg: 0,
        gain_back_db: 0.0,
        enabled: true
      }
    ]
  },
  object: {
    class: "object",
    enabled: true,
    size_m: {
      l: 0.6,
      w: 0.4,
      h: 0.4
    },
    critical_dim_m: null,
    motion: {
      mode: "static",
      speed_mps: 0.0,
      speed_max_mps: 0.0,
      accel_mps2: 1.0,
      turn_rate_max_rad_s: 0.5
    },
    albedo_vis: 0.3,
    contrast0: 0.3,
    camouflage: 0.0,
    occlusion: 0.0,
    reflect_850: 0.3,
    reflect_905: 0.3,
    thermal: {
      mode: "relative",
      delta_t_k: 0.5,
      emissivity: 0.9,
      hot_fraction: 1.0
    },
    rcs_m2: 0.3,
    rcs_fluct: "swerling0",
    rcs_band: "77ghz",
    sound: null,
    rf: null,
    criterion_mode: "geometric",
    alertness: 0.0,
    hearing_th_dba: 20,
    acoustic_adv_db: 0,
    visual_theta0_arcmin: 1.0,
    level_req: "R",
    n_req_px: null,
    reaction: {
      onset: {
        kind: "none",
        duration_s: 0
      },
      response: [
        {
          kind: "ignore"
        }
      ],
      alert_others: false
    },
    calm_s: 60,
    alert_radius_m: 50,
    sensitivity: []
  }
} as const
