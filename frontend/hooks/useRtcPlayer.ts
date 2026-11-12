'use client'

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { RtcSession, transportOf, type RtcPeer, type RtcPeerEvents } from '@/lib/chat/rtcSession'
import type { IceServer, MediaCapabilities, RtcSignal } from '@/lib/types'

/** RTCPeerConnection behind the narrow interface RtcSession drives. */
function browserPeer(iceServers: IceServer[], events: RtcPeerEvents<MediaStreamTrack>): RtcPeer {
  const pc = new RTCPeerConnection({ iceServers })
  pc.ontrack = (event) => events.onTrack(event.track)
  pc.onconnectionstatechange = () => events.onConnectionState(pc.connectionState)
  pc.onicegatheringstatechange = () => events.onIceGatheringState(pc.iceGatheringState)
  return {
    addTransceiver: (kind, init) => {
      pc.addTransceiver(kind, init)
    },
    createOffer: async () => ({ type: 'offer', sdp: (await pc.createOffer()).sdp }),
    setLocalDescription: (description) => pc.setLocalDescription(description),
    setRemoteDescription: (description) => pc.setRemoteDescription(description),
    localSdp: () => pc.localDescription?.sdp ?? null,
    iceGatheringState: () => pc.iceGatheringState,
    close: () => {
      pc.ontrack = null
      pc.onconnectionstatechange = null
      pc.onicegatheringstatechange = null
      pc.close()
    },
  }
}

// How many times a stream that pauses while already muted is resumed before
// it is left alone. Reset whenever it does play. Without a limit, a browser
// that refuses outright (a backgrounded iOS tab) is asked in a loop.
const MAX_MUTED_RESUMES = 2

export interface RtcPlayer {
  /** The server has announced the stream: sentences arrive on it. */
  live: boolean
  /** The stream element has a decoded frame to show. */
  hasPicture: boolean
  /** The browser would not play the stream's sound without a tap. */
  audioBlocked: boolean
  /** The socket's `ready` frame: a new connection, so a new negotiation. */
  ready(media: MediaCapabilities | undefined): void
  answer(sdp: string): void
  serverError(): void
  mediaTransport(transport: 'webrtc' | 'clips'): void
  socketClosed(): void
  /** Keep the stream silent (user mute, a stop in progress, a clip over it). */
  setSilenced(silenced: boolean): void
  /** The user asked for sound, from a gesture. */
  enableAudio(): void
}

interface Options {
  /** Send a signalling frame on the chat socket. False if it was not open. */
  send(signal: RtcSignal): boolean
  /** The transport changed. Called synchronously, before any later frame. */
  onLive(live: boolean): void
}

/**
 * The WebRTC half of the avatar's output: one peer connection, and the
 * <video> its stream plays in.
 *
 * The peer connection's life is tied to the SOCKET connection, not to this
 * component: `ready()` on each (re)connect starts a negotiation and discards
 * whatever the previous socket had, and `socketClosed()` tears it down. All of
 * that lives in RtcSession; this hook supplies the browser pieces and owns
 * the element.
 */
export function useRtcPlayer(streamRef: RefObject<HTMLVideoElement | null>, options: Options): RtcPlayer {
  const [live, setLive] = useState(false)
  const [hasPicture, setHasPicture] = useState(false)
  const [audioBlocked, setAudioBlockedState] = useState(false)
  const audioBlockedRef = useRef(false)

  const optionsRef = useRef(options)
  useLayoutEffect(() => {
    optionsRef.current = options
  })
  const sessionRef = useRef<RtcSession<MediaStreamTrack> | null>(null)

  const element = useMemo(() => {
    // Both tracks go into ONE stream so the element keeps them in sync.
    let stream: MediaStream | null = null
    let silenced = false
    let mutedResumes = 0

    const wantMuted = () => silenced || audioBlockedRef.current

    function setAudioBlocked(next: boolean) {
      if (audioBlockedRef.current === next) return
      audioBlockedRef.current = next
      setAudioBlockedState(next)
    }

    function play(el: HTMLVideoElement) {
      // Same approach as the clip player: muted autoplay is always allowed,
      // so start that way and restore sound once it is running. Where that
      // un-mute pauses the element (WebKit, no gesture), onPause takes over.
      const playing = stream
      el.muted = true
      el.play()
        .then(() => {
          if (stream === playing) el.muted = wantMuted()
        })
        // Nothing to skip to — a stream has no next clip. The picture stays
        // on the idle still until a tap or the next track retries.
        .catch(() => {})
    }

    return {
      attach(track: MediaStreamTrack) {
        if (!stream) stream = new MediaStream()
        stream.addTrack(track)
        const el = streamRef.current
        if (!el) return
        if (el.srcObject !== stream) el.srcObject = stream
        if (el.paused) play(el)
      },
      detach() {
        const old = stream
        stream = null
        mutedResumes = 0
        old?.getTracks().forEach((track) => track.stop())
        const el = streamRef.current
        if (el && el.srcObject) el.srcObject = null
        setHasPicture(false)
        // A blocked stream is gone; the next one gets a fresh attempt.
        setAudioBlocked(false)
      },
      setSilenced(next: boolean) {
        silenced = next
        const el = streamRef.current
        if (!el || !stream) return
        if (next) el.muted = true
        // While it is still in its muted start, the play() continuation
        // applies the choice.
        else if (!el.paused) el.muted = wantMuted()
      },
      enableAudio() {
        setAudioBlocked(false)
        const el = streamRef.current
        if (!el || !stream) return
        el.muted = wantMuted()
        mutedResumes = 0
        if (el.paused) el.play().catch(() => {})
      },
      /** The element paused although a stream is attached. */
      onPause(el: HTMLVideoElement) {
        if (!stream || el.srcObject !== stream || !el.paused) return
        if (!el.muted) {
          // The autoplay policy paused it for being audible without a
          // gesture. Play it muted and ask for a tap.
          setAudioBlocked(true)
          el.muted = true
          el.play().catch(() => {})
          return
        }
        if (mutedResumes >= MAX_MUTED_RESUMES) return
        mutedResumes += 1
        el.play().catch(() => {})
      },
      onFrame(el: HTMLVideoElement) {
        if (!stream || el.srcObject !== stream) return
        if (!el.paused) mutedResumes = 0
        // videoWidth, not the event: with the audio track attached first the
        // element reports `playing` before there is any picture.
        if (el.videoWidth > 0) setHasPicture(true)
      },
    }
  }, [streamRef])

  useEffect(() => {
    const el = streamRef.current
    if (!el) return
    const onPause = () => element.onPause(el)
    const onFrame = () => element.onFrame(el)
    el.addEventListener('pause', onPause)
    el.addEventListener('playing', onFrame)
    el.addEventListener('loadeddata', onFrame)
    el.addEventListener('resize', onFrame)
    return () => {
      el.removeEventListener('pause', onPause)
      el.removeEventListener('playing', onFrame)
      el.removeEventListener('loadeddata', onFrame)
      el.removeEventListener('resize', onFrame)
    }
  }, [element, streamRef])

  // Created in an effect so that StrictMode's throwaway first mount disposes
  // of its own session. No peer connection exists until a `ready` asks for
  // one, so that first mount never opens anything.
  useEffect(() => {
    const session = new RtcSession<MediaStreamTrack>({
      supported: () => typeof RTCPeerConnection !== 'undefined',
      createPeer: browserPeer,
      send: (signal) => optionsRef.current.send(signal),
      attach: element.attach,
      detach: element.detach,
      onChange: (state) => {
        const next = transportOf(state) === 'webrtc'
        setLive(next)
        optionsRef.current.onLive(next)
      },
    })
    sessionRef.current = session
    return () => {
      sessionRef.current = null
      // Tracks stopped, peer closed, element cleared — and `webrtc_close`
      // if the socket is still open to carry it.
      session.dispose()
      setLive(false)
      optionsRef.current.onLive(false)
    }
  }, [element])

  return useMemo<RtcPlayer>(
    () => ({
      live,
      hasPicture,
      audioBlocked,
      ready: (media) => sessionRef.current?.ready(media),
      answer: (sdp) => sessionRef.current?.answer(sdp),
      serverError: () => sessionRef.current?.serverError(),
      mediaTransport: (transport) => sessionRef.current?.mediaTransport(transport),
      socketClosed: () => sessionRef.current?.socketClosed(),
      setSilenced: element.setSilenced,
      enableAudio: element.enableAudio,
    }),
    [live, hasPicture, audioBlocked, element],
  )
}
