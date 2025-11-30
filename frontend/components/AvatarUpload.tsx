'use client'

import { useState, useCallback } from 'react'
import { Upload, Loader2 } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'

export function AvatarUpload() {
  const [dragActive, setDragActive] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  const [name, setName] = useState('')
  const queryClient = useQueryClient()

  const uploadMutation = useMutation({
    mutationFn: (formData: FormData) => api.uploadAvatar(formData),
    onSuccess: () => {
      toast.success('Avatar uploaded successfully!')
      setPreview(null)
      setName('')
      queryClient.invalidateQueries({ queryKey: ['avatars'] })
    },
    onError: () => {
      toast.error('Failed to upload avatar')
    },
  })

  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true)
    } else if (e.type === 'dragleave') {
      setDragActive(false)
    }
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)

    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      handleFile(e.dataTransfer.files[0])
    }
  }, [])

  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    e.preventDefault()
    if (e.target.files && e.target.files[0]) {
      handleFile(e.target.files[0])
    }
  }, [])

  const handleFile = (file: File) => {
    if (!file.type.startsWith('image/')) {
      toast.error('Please upload an image file')
      return
    }

    const reader = new FileReader()
    reader.onload = (e) => {
      setPreview(e.target?.result as string)
    }
    reader.readAsDataURL(file)
  }

  const handleSubmit = () => {
    if (!preview || !name) {
      toast.error('Please provide a name and select an image')
      return
    }

    fetch(preview)
      .then(res => res.blob())
      .then(blob => {
        const formData = new FormData()
        formData.append('file', blob, 'avatar.jpg')
        formData.append('name', name)
        uploadMutation.mutate(formData)
      })
  }

  return (
    <div className="card">
      <h2 className="text-2xl font-semibold mb-4">Upload Avatar</h2>

      <div className="mb-4">
        <label className="block text-sm font-medium text-gray-700 mb-2">
          Avatar Name
        </label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-primary-500 focus:border-transparent"
          placeholder="My Avatar"
        />
      </div>

      <div
        className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors ${
          dragActive
            ? 'border-primary-500 bg-primary-50'
            : 'border-gray-300 hover:border-primary-400'
        }`}
        onDragEnter={handleDrag}
        onDragLeave={handleDrag}
        onDragOver={handleDrag}
        onDrop={handleDrop}
      >
        <input
          type="file"
          id="avatar-upload"
          accept="image/*"
          onChange={handleChange}
          className="hidden"
        />

        {preview ? (
          <div className="space-y-4">
            <img
              src={preview}
              alt="Preview"
              className="max-w-xs mx-auto rounded-lg"
            />
            <button
              onClick={() => setPreview(null)}
              className="btn-secondary"
            >
              Change Image
            </button>
          </div>
        ) : (
          <label htmlFor="avatar-upload" className="cursor-pointer">
            <Upload className="mx-auto mb-4 text-gray-400" size={48} />
            <p className="text-lg font-medium text-gray-700 mb-2">
              Drop your avatar image here
            </p>
            <p className="text-sm text-gray-500">
              or click to browse (JPG, PNG, WEBP)
            </p>
          </label>
        )}
      </div>

      {preview && (
        <button
          onClick={handleSubmit}
          disabled={uploadMutation.isPending}
          className="mt-4 w-full btn-primary flex items-center justify-center gap-2"
        >
          {uploadMutation.isPending ? (
            <>
              <Loader2 className="animate-spin" size={20} />
              Uploading...
            </>
          ) : (
            'Upload Avatar'
          )}
        </button>
      )}
    </div>
  )
}
