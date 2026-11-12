import { describe, expect, it } from 'vitest'
import { detailText, errorMessage, errorStatus } from '@/lib/errors'
import { emailProblem, passwordProblem, usernameProblem } from '@/lib/validation'

const apiError = (status: number, detail: unknown) => ({ response: { status, data: { detail } } })

describe('errorMessage', () => {
  it('passes a string detail through', () => {
    expect(errorMessage(apiError(403, 'Current password is incorrect'), 'fallback'))
      .toBe('Current password is incorrect')
  })

  it('flattens a FastAPI validation array into a sentence, never an object', () => {
    // The exact shape a 422 carries. Handing this array to a toast crashed the app.
    const detail = [
      { type: 'string_pattern_mismatch', loc: ['body', 'username'], msg: "String should match pattern '^[A-Za-z0-9]…'", input: 'a b' },
      { type: 'value_error', loc: ['body', 'email'], msg: 'value is not a valid email address: An email address must have an @-sign.', input: 'nope' },
    ]
    const text = errorMessage(apiError(422, detail), 'fallback')
    expect(typeof text).toBe('string')
    expect(text).toBe(
      "Username: String should match pattern '^[A-Za-z0-9]…' · Email: value is not a valid email address: An email address must have an @-sign.",
    )
  })

  it('strips the "Value error," prefix pydantic adds to custom validators', () => {
    const detail = [{ loc: ['body', 'password'], msg: 'Value error, Password is too common' }]
    expect(errorMessage(apiError(422, detail), 'x')).toBe('Password: Password is too common')
  })

  it('labels a field it has no name for, and omits the label for a bare body error', () => {
    expect(detailText([{ loc: ['body', 'voice_name'], msg: 'Field required' }])).toBe('Voice name: Field required')
    expect(detailText([{ loc: ['body'], msg: 'Invalid JSON' }])).toBe('Invalid JSON')
  })

  it.each([
    ['no response at all (network error)', { message: 'Network Error' }],
    ['a null detail', apiError(500, null)],
    ['an empty string', apiError(500, '   ')],
    ['an empty array', apiError(422, [])],
    ['an array of junk', apiError(422, [null, 42, {}, { msg: '' }])],
    ['a number', apiError(500, 500)],
    ['undefined', undefined],
    ['null', null],
    ['a string thrown as an error', 'boom'],
  ])('falls back for %s', (_label, err) => {
    expect(errorMessage(err, 'Something went wrong')).toBe('Something went wrong')
  })

  it('always returns a string, whatever it is given', () => {
    const inputs: unknown[] = [
      apiError(422, [{ loc: ['body', 'x'], msg: 'bad' }]), apiError(400, { msg: 'nested' }),
      apiError(400, { message: 'nested too' }), apiError(400, { other: 1 }), apiError(400, [['nested', 'array']]),
      new Error('x'), 0, '', [],
    ]
    for (const input of inputs) expect(typeof errorMessage(input, 'fallback')).toBe('string')
    expect(errorMessage(apiError(400, { msg: 'nested' }), 'f')).toBe('nested')
  })
})

describe('errorStatus', () => {
  it('reads the HTTP status, or undefined when there was no response', () => {
    expect(errorStatus(apiError(401, 'x'))).toBe(401)
    expect(errorStatus({ message: 'Network Error' })).toBeUndefined()
    expect(errorStatus(null)).toBeUndefined()
  })
})

describe('account validation mirrors the server rules', () => {
  it('email', () => {
    expect(emailProblem('')).toBe('Enter an email address')
    expect(emailProblem('nope')).not.toBeNull()
    expect(emailProblem('a@b')).not.toBeNull()
    expect(emailProblem('a b@c.com')).not.toBeNull()
    expect(emailProblem('  me@example.com ')).toBeNull()
  })

  it('username: 3–39 chars, letters digits - _, alphanumeric at both ends', () => {
    for (const bad of ['', 'ab', 'a'.repeat(40), 'a b', '-abc', 'abc-', '_abc', 'abc_', 'a.b.c', 'héllo']) {
      expect(usernameProblem(bad), bad).not.toBeNull()
    }
    for (const good of ['abc', 'a-b', 'a_b', 'A1b2', 'a'.repeat(39), 'user-name_1']) {
      expect(usernameProblem(good), good).toBeNull()
    }
  })

  it('password: 8–128 characters', () => {
    expect(passwordProblem('1234567')).not.toBeNull()
    expect(passwordProblem('12345678')).toBeNull()
    expect(passwordProblem('x'.repeat(128))).toBeNull()
    expect(passwordProblem('x'.repeat(129))).not.toBeNull()
  })
})
