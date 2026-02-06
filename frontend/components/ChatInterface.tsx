'use client'

import { useState, useEffect, useRef } from 'react'
import { Send, Mic, MicOff, Video, Loader2 } from 'lucide-react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'

interface ChatInterfaceProps {
  avatarId: string
}

export function ChatInterface({ avatarId }: ChatInterfaceProps) {
  const [messages, setMessages] = useState<any[]>([])
  const [inputText, setInputText] = useState('')
  const [isRecording, setIsRecording] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [ws, setWs] = useState<WebSocket | null>(null)
  const [currentVideo, setCurrentVideo] = useState<string | null>(null)
  const [isProcessing, setIsProcessing] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)

  // Create session
  const createSessionMutation = useMutation({
    mutationFn: () => api.createSession(avatarId),
    onSuccess: (data) => {
      setSessionId(data.id)
      connectWebSocket(data.id)
      toast.success('Session started!')
    },
    onError: () => {
      toast.error('Failed to start session')
    },
  })

  // Connect to WebSocket
  const connectWebSocket = (sessionId: string) => {
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000'
    const websocket = new WebSocket(`${wsUrl}/ws/session/${sessionId}`)

    websocket.onopen = () => {
      console.log('WebSocket connected')
    }

    websocket.onmessage = (event) => {
      const data = JSON.parse(event.data)
      handleWebSocketMessage(data)
    }

    websocket.onerror = (error) => {
      console.error('WebSocket error:', error)
      toast.error('Connection error')
    }

    websocket.onclose = () => {
      console.log('WebSocket disconnected')
    }

    setWs(websocket)
  }

  // Handle WebSocket messages
  const handleWebSocketMessage = (data: any) => {
    console.log('Received:', data)

    switch (data.type) {
      case 'transcription':
        setMessages(prev => [...prev, {
          role: 'user',
          content: data.text,
          timestamp: new Date()
        }])
        break

      case 'message':
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: data.content,
          timestamp: new Date()
        }])
        break

      case 'video':
        setCurrentVideo(data.video_url)
        setIsProcessing(false)
        if (videoRef.current) {
          videoRef.current.src = data.video_url
          videoRef.current.play()
        }
        break

      case 'status':
        setIsProcessing(true)
        toast.loading(data.message, { id: 'processing' })
        break

      case 'error':
        toast.error(data.message)
        setIsProcessing(false)
        break
    }
  }

  // Send text message
  const sendMessage = () => {
    if (!inputText.trim() || !ws || !sessionId) return

    const message = {
      type: 'text',
      text: inputText
    }

    ws.send(JSON.stringify(message))
    setMessages(prev => [...prev, {
      role: 'user',
      content: inputText,
      timestamp: new Date()
    }])
    setInputText('')
    setIsProcessing(true)
  }

  // Start voice recording
  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mediaRecorder = new MediaRecorder(stream)
      const audioChunks: Blob[] = []

      mediaRecorder.ondataavailable = (event) => {
        audioChunks.push(event.data)
      }

      mediaRecorder.onstop = async () => {
        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' })
        const reader = new FileReader()
        
        reader.onloadend = () => {
          const base64Audio = (reader.result as string).split(',')[1]
          
          if (ws) {
            ws.send(JSON.stringify({
              type: 'audio',
              audio: base64Audio
            }))
            setIsProcessing(true)
          }
        }
        
        reader.readAsDataURL(audioBlob)
        stream.getTracks().forEach(track => track.stop())
      }

      mediaRecorder.start()
      mediaRecorderRef.current = mediaRecorder
      setIsRecording(true)
      toast.success('Recording started')
    } catch (error) {
      toast.error('Failed to access microphone')
    }
  }

  // Stop voice recording
  const stopRecording = () => {
    if (mediaRecorderRef.current) {
      mediaRecorderRef.current.stop()
      setIsRecording(false)
      toast.success('Recording stopped')
    }
  }

  // Initialize session
  useEffect(() => {
    createSessionMutation.mutate()

    return () => {
      if (ws) {
        ws.close()
      }
    }
  }, [])

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* Video Panel */}
      <div className="card">
        <h2 className="text-2xl font-semibold mb-4">Avatar</h2>
        <div className="aspect-video bg-gray-900 rounded-lg overflow-hidden relative">
          {currentVideo ? (
            <video
              ref={videoRef}
              className="w-full h-full object-cover"
              controls
              autoPlay
            />
          ) : (
            <div className="flex items-center justify-center h-full">
              <div className="text-center text-gray-400">
                <Video size={64} className="mx-auto mb-4 opacity-50" />
                <p>Avatar will appear here</p>
              </div>
            </div>
          )}
          
          {isProcessing && (
            <div className="absolute inset-0 bg-black bg-opacity-50 flex items-center justify-center">
              <div className="text-center text-white">
                <Loader2 className="animate-spin mx-auto mb-2" size={48} />
                <p>Processing...</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Chat Panel */}
      <div className="card flex flex-col h-[600px]">
        <h2 className="text-2xl font-semibold mb-4">Conversation</h2>
        
        {/* Messages */}
        <div className="flex-1 overflow-y-auto space-y-4 mb-4">
          {messages.length === 0 ? (
            <div className="text-center text-gray-500 py-8">
              <p>Start a conversation with your avatar!</p>
            </div>
          ) : (
            messages.map((message, index) => (
              <div
                key={index}
                className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`max-w-[80%] rounded-lg px-4 py-2 ${
                    message.role === 'user'
                      ? 'bg-primary-600 text-white'
                      : 'bg-gray-200 text-gray-900'
                  }`}
                >
                  <p className="text-sm">{message.content}</p>
                  <p className="text-xs mt-1 opacity-70">
                    {new Date(message.timestamp).toLocaleTimeString()}
                  </p>
                </div>
              </div>
            ))
          )}
        </div>

        {/* Input */}
        <div className="flex gap-2">
          <button
            onClick={isRecording ? stopRecording : startRecording}
            disabled={isProcessing}
            className={`p-3 rounded-lg transition-colors ${
              isRecording
                ? 'bg-red-600 hover:bg-red-700 text-white animate-pulse'
                : 'bg-gray-200 hover:bg-gray-300 text-gray-800'
            }`}
          >
            {isRecording ? <MicOff size={20} /> : <Mic size={20} />}
          </button>
          
          <input
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && sendMessage()}
            placeholder="Type your message..."
            disabled={isProcessing}
            className="flex-1 px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
          />
          
          <button
            onClick={sendMessage}
            disabled={!inputText.trim() || isProcessing}
            className="btn-primary px-6"
          >
            <Send size={20} />
          </button>
        </div>
      </div>
    </div>
  )
}
