import axios from 'axios'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export const api = {
  // Avatars
  uploadAvatar: async (formData: FormData) => {
    const response = await axios.post(`${API_URL}/api/v1/avatars/upload`, formData)
    return response.data
  },

  getAvatars: async () => {
    const response = await axios.get(`${API_URL}/api/v1/avatars/`)
    return response.data
  },

  deleteAvatar: async (avatarId: string) => {
    const response = await axios.delete(`${API_URL}/api/v1/avatars/${avatarId}`)
    return response.data
  },

  // Sessions
  createSession: async (avatarId: string) => {
    const response = await axios.post(`${API_URL}/api/v1/sessions/create`, {
      avatar_id: avatarId,
    })
    return response.data
  },

  // Messages
  sendMessage: async (sessionId: string, content: string) => {
    const response = await axios.post(`${API_URL}/api/v1/messages/send`, {
      session_id: sessionId,
      content,
    })
    return response.data
  },

  getMessages: async (sessionId: string) => {
    const response = await axios.get(`${API_URL}/api/v1/messages/session/${sessionId}`)
    return response.data
  },
}
