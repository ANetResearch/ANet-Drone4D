import { describe, expect, it } from 'vitest'
import { canToggleVisibility, canWithdraw, DIGEST_RE, parseCmd, parseEnv } from '@/ui/lab/submissions'

describe('submission form helpers', () => {
  it('parses the entry command', () => {
    expect(parseCmd('python -m drone4d.serve my_pkg.agent:Agent')).toEqual(['python', '-m', 'drone4d.serve', 'my_pkg.agent:Agent'])
    expect(parseCmd('["python", "-m", "a b"]')).toEqual(['python', '-m', 'a b'])
    expect(parseCmd('[1, 2]')).toBeNull()
    expect(parseCmd('   ')).toBeNull()
    expect(parseCmd('[broken')).toBeNull()
  })
  it('parses the environment and rejects reserved or malformed keys', () => {
    expect(parseEnv('MODE=eval\n\nLEVEL=a=b')).toEqual({ MODE: 'eval', LEVEL: 'a=b' })
    expect(parseEnv('mode=eval')).toBeNull()
    expect(parseEnv('DRONE4D_AGENT_SOCK=/x')).toBeNull()
    expect(parseEnv('NOVALUE')).toBeNull()
    expect(parseEnv('')).toEqual({})
  })
  it('digest pattern and state rules', () => {
    expect(DIGEST_RE.test(`sha256:${'a'.repeat(64)}`)).toBe(true)
    expect(DIGEST_RE.test('d3-agent-py:dev')).toBe(false)
    expect(canWithdraw('VALIDATING')).toBe(true)
    expect(canWithdraw('EVALUATING')).toBe(false)
    expect(canToggleVisibility('DONE')).toBe(true)
    expect(canToggleVisibility('REJECTED')).toBe(false)
  })
})
