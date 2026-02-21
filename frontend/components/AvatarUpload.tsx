'use client'

import { useState, useCallback } from 'react'
import { Upload, Loader2, X, CheckCircle2, ImagePlus, Sparkles, AlertCircle } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'

export function AvatarUpload() {
  const [dragActive, setDragActive] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  const [fileName, setFileName] = useState<string>('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  const uploadMutation = useMutation({
    mutationFn: (formData: FormData) => api.uploadAvatar(formData),
    onSuccess: () => {
      toast.success('Avatar uploaded!', { icon: '✨' })
      setPreview(null)
      setName('')
      setFileName('')
      setError(null)
      queryClient.invalidateQueries({ queryKey: ['avatars'] })
    },
    onError: () => {
      toast.error('Upload failed — please try again')
    },
  })

  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(e.type === 'dragenter' || e.type === 'dragover')
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    if (e.dataTransfer.files?.[0]) processFile(e.dataTransfer.files[0])
  }, [])

  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.[0]) processFile(e.target.files[0])
  }, [])

  const processFile = (file: File) => {
    setError(null)

    if (!file.type.startsWith('image/')) {
      setError('Please upload a JPG, PNG, or WEBP image.')
      return
    }

    if (file.size > 10 * 1024 * 1024) {
      setError('File must be under 10 MB.')
      return
    }

    setFileName(file.name)
    if (!name) setName(file.name.replace(/\.[^/.]+$/, ''))

    const reader = new FileReader()
    reader.onload = (e) => setPreview(e.target?.result as string)
    reader.readAsDataURL(file)
  }

  const handleSubmit = () => {
    if (!preview || !name.trim()) {
      setError('Please give your avatar a name.')
      return
    }

    fetch(preview)
      .then(res => res.blob())
      .then(blob => {
        const formData = new FormData()
        formData.append('file', blob, fileName || 'avatar.jpg')
        formData.append('name', name.trim())
        uploadMutation.mutate(formData)
      })
  }

  const clearPreview = () => {
    setPreview(null)
    setFileName('')
    setError(null)
  }

  return (
    <div className="card flex flex-col gap-5">
      {/* Header */}
      <div>
        <h2 className="text-xl font-bold text-white">Upload Avatar</h2>
        <p className="text-sm text-gray-500 mt-0.5">JPG · PNG · WEBP · up to 10 MB</p>
      </div>

      <div className="divider" />

      {/* Name field */}
      <div className="space-y-1.5">
        <label className="text-sm font-medium text-gray-300">Avatar Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => { setName(e.target.value); setError(null) }}
          className="input-field"
          placeholder="e.g. Alex, News Anchor, CEO…"
          maxLength={60}
        />
      </div>

      {/* Drop zone */}
      {!preview ? (
        <div
          onDragEnter={handleDrag}
          onDragLeave={handleDrag}
          onDragOver={handleDrag}
          onDrop={handleDrop}
          className={`relative rounded-2xl border-2 border-dashed p-10 text-center cursor-pointer
            transition-all duration-300
            ${dragActive
              ? 'border-primary-400 bg-primary-500/10 scale-[1.01] shadow-glow-sm'
              : 'border-white/15 hover:border-primary-500/50 hover:bg-primary-500/5'
            }`}
        >
          <input
            type="file"
            id="avatar-upload"
            accept="image/*"
            onChange={handleChange}
            className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
          />

          <div className="pointer-events-none flex flex-col items-center gap-4">
            {/* Icon */}
            <div className={`w-16 h-16 rounded-2xl flex items-center justify-center border transition-all duration-300
              ${dragActive
                ? 'bg-primary-500/20 border-primary-400/50'
                : 'bg-surface-700/80 border-white/10'
              }`}
            >
              {dragActive ? (
                <Sparkles size={28} className="text-primary-400 animate-pulse" />
              ) : (
                <ImagePlus size={28} className="text-gray-400" />
              )}
            </div>

            <div>
              <p className="text-white font-semibold text-base mb-1">
                {dragActive ? 'Drop to upload' : 'Drag & drop your photo'}
              </p>
              <p className="text-gray-500 text-sm">
                or <span className="text-primary-400 font-medium underline underline-offset-2">click to browse</span>
              </p>
            </div>

            {/* Tips */}
            <div className="flex flex-wrap justify-center gap-2 mt-2">
              {['Clear face', 'Good lighting', 'Front-facing'].map(tip => (
                <span key={tip} className="badge-purple text-xs">{tip}</span>
              ))}
            </div>
          </div>
        </div>
      ) : (
        /* Preview */
        <div className="relative rounded-2xl overflow-hidden border border-white/10 group">
          <img
            src={preview}
            alt="Avatar preview"
            className="w-full max-h-64 object-cover"
          />
          {/* Overlay */}
          <div className="absolute inset-0 bg-gradient-to-t from-surface-950/90 via-surface-950/20 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300" />

          {/* Remove button */}
          <button
            onClick={clearPreview}
            className="absolute top-3 right-3 w-8 h-8 rounded-full bg-surface-900/80 backdrop-blur-sm border border-white/15
                       flex items-center justify-center text-gray-400 hover:text-white hover:border-red-500/40
                       transition-all duration-200 opacity-0 group-hover:opacity-100"
          >
            <X size={15} />
          </button>

          {/* File name tag */}
          <div className="absolute bottom-3 left-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg
                          bg-surface-900/80 backdrop-blur-sm border border-white/10">
            <CheckCircle2 size={13} className="text-green-400" />
            <span className="text-xs text-gray-300 truncate max-w-[180px]">{fileName}</span>
          </div>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="flex items-center gap-2 px-3 py-2.5 rounded-xl bg-red-500/10 border border-red-500/20 animate-slide-up">
          <AlertCircle size={14} className="text-red-400 flex-shrink-0" />
          <p className="text-sm text-red-400">{error}</p>
        </div>
      )}

      {/* Upload button */}
      {preview && (
        <button
          onClick={handleSubmit}
          disabled={uploadMutation.isPending || !name.trim()}
          className="btn-primary w-full py-3.5 text-base rounded-xl animate-slide-up"
        >
          {uploadMutation.isPending ? (
            <>
              <Loader2 size={18} className="animate-spin" />
              Uploading & Processing…
            </>
          ) : (
            <>
              <Upload size={18} />
              Upload Avatar
            </>
          )}
        </button>
      )}

      {/* Processing note */}
      {uploadMutation.isPending && (
        <p className="text-xs text-center text-gray-500 animate-pulse">
          Detecting face · Cropping · Optimizing…
        </p>
      )}
    </div>
  )
}
