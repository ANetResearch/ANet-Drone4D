// engine/perception (M19; M18-M19 PRD §7.8, §15): the perception calculator (TS port of the Python reference: GSD, pixels,
// TTPF, the unified criterion, zoom for a level, the feasible station window, reference ranges and the instant hints of the
// clone form and the payload controls), the perception parameters (contract literal copy) and the wire helpers of the
// perception data products (frame decoding, HUD line, capture boxes); D2.1: the pair quantities of the readout (quant.ts)
// and the field view and perception volumes (field.ts: decoders, interpolation, body mask, page model). Owner: M19.
// Framework-free (no React, no store).
export * from './levels'
export * from './calc'
export * from './decode'
export * from './quant'
export * from './field'
