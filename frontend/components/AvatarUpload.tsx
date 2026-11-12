'use client'

import { useEffect, useState, type ChangeEvent, type DragEvent } from 'react'
import { Upload, Loader2, X, CheckCircle2, ImagePlus, Sparkles, AlertCircle } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'react-hot-toast'
import { api } from '@/lib/api'

const MAX_BYTES = 10 * 1024 * 1024

export function AvatarUpload() {
  const [dragActive, setDragActive] = useState(false)
  // The chosen File is kept as-is and uploaded directly. It used to be read
  // into a data: URL for the preview and then fetched back out of that URL to
  // rebuild a blob for upload — a base64 round trip of up to 10 MB for no gain.
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  // Object URLs hold the file in memory until revoked.
  useEffect(() => {
    if (!file) {
      setPreview(null)
      return
    }
    const url = URL.createObjectURL(file)
    setPreview(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const uploadMutation = useMutation({
    mutationFn: (formData: FormData) => api.uploadAvatar(formData),
    onSuccess: () => {
      toast.success('Avatar uploaded!', { icon: '✨' })
      setFile(null)
      setName('')
      setError(null)
      queryClient.invalidateQueries({ queryKey: ['avatars'] })
    },
    onError: () => {
      toast.error('Upload failed — please try again')
    },
  })

  const processFile = (picked: File) => {
    setError(null)
    if (!picked.type.startsWith('image/')) {
      setError('Please upload a JPG, PNG, or WEBP image.')
      return
    }
    if (picked.size > MAX_BYTES) {
      setError('File must be under 10 MB.')
      return
    }
    setFile(picked)
    // Functional update: only fill the name in if the user has not typed one.
    // Reading `name` from a memoised handler captured the first render's
    // empty string, so a typed name was overwritten by the filename on drop.
    setName((current) => current || picked.name.replace(/\.[^/.]+$/, ''))
  }

  const handleDrag = (e: DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(e.type === 'dragenter' || e.type === 'dragover')
  }

  const handleDrop = (e: DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    if (e.dataTransfer.files?.[0]) processFile(e.dataTransfer.files[0])
  }

  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.[0]) processFile(e.target.files[0])
    // Allow picking the same file again after clearing it.
    e.target.value = ''
  }

  const handleSubmit = () => {
    if (!file || !name.trim()) {
      setError('Please give your avatar a name.')
      return
    }
    const formData = new FormData()
    formData.append('file', file, file.name || 'avatar.jpg')
    formData.append('name', name.trim())
    uploadMutation.mutate(formData)
  }

  const clearPreview = () => {
    setFile(null)
    setError(null)
  }

  return (
    <div className="card flex flex-col gap-5">
      <div>
        <h2 className="text-xl text-gray-900">Upload Avatar</h2>
        <p className="mt-0.5 text-sm text-gray-500">JPG · PNG · WEBP · up to 10 MB</p>
      </div>

      <div className="divider" />

      <div className="space-y-1.5">
        <label htmlFor="avatar-name" className="text-sm font-medium text-gray-700">Avatar Name</label>
        <input
          id="avatar-name"
          type="text"
          value={name}
          onChange={(e) => { setName(e.target.value); setError(null) }}
          className="input-field"
          placeholder="e.g. Alex, News Anchor, CEO…"
          maxLength={60}
        />
      </div>

      {!preview ? (
        <div
          onDragEnter={handleDrag}
          onDragLeave={handleDrag}
          onDragOver={handleDrag}
          onDrop={handleDrop}
          className={`relative cursor-pointer rounded-xl border-2 border-dashed p-10 text-center transition-colors duration-200
 focus-within:border-primary-600 focus-within:shadow-ring
 ${dragActive
   ? 'border-primary-600 bg-primary-50'
   : 'border-gray-300 hover:border-gray-400 hover:bg-gray-50'}`}
        >
          <input
            type="file"
            id="avatar-upload"
            accept="image/*"
            onChange={handleChange}
            aria-label="Choose an avatar photo"
            className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
          />

          <div className="pointer-events-none flex flex-col items-center gap-4">
            <div className="flex h-16 w-16 items-center justify-center rounded-xl border border-gray-200 bg-gray-50">
              {dragActive ? (
                <Sparkles size={28} className="text-primary-600" aria-hidden="true" />
              ) : (
                <ImagePlus size={28} className="text-gray-500" aria-hidden="true" />
              )}
            </div>

            <div>
              <p className="mb-1 text-base font-semibold text-gray-900">
                {dragActive ? 'Drop to upload' : 'Drag & drop your photo'}
              </p>
              <p className="text-sm text-gray-500">
                or <span className="font-medium text-primary-600 underline underline-offset-2">click to browse</span>
              </p>
            </div>

            <div className="mt-2 flex flex-wrap justify-center gap-2">
              {['Clear face', 'Good lighting', 'Front-facing'].map((tip) => (
                <span key={tip} className="badge-gray">{tip}</span>
              ))}
            </div>
          </div>
        </div>
      ) : (
        <div className="relative overflow-hidden rounded-xl border border-gray-200">
          {/* A local object URL — nothing for next/image to optimise. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={preview} alt="Avatar preview" className="max-h-64 w-full object-cover" />

          {/* Always visible: revealed only on hover, it could not be reached
              by keyboard or on a touch screen. */}
          <button
            onClick={clearPreview}
            aria-label="Remove selected photo"
            className="absolute right-3 top-3 flex h-8 w-8 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-600 shadow-xs transition-colors hover:text-gray-900"
          >
            <X size={15} />
          </button>

          <div className="absolute bottom-3 left-3 flex items-center gap-1.5 rounded-md border border-gray-200 bg-white px-3 py-1.5 shadow-xs">
            <CheckCircle2 size={13} className="text-success-600" aria-hidden="true" />
            <span className="max-w-[180px] truncate text-xs text-gray-700">{file?.name}</span>
          </div>
        </div>
      )}

      {error && (
        <div role="alert" className="flex animate-slide-up items-center gap-2 rounded-md bg-error-50 px-3 py-2.5 ring-1 ring-inset ring-error-700/[.12]">
          <AlertCircle size={14} className="shrink-0 text-error-700" aria-hidden="true" />
          <p className="text-sm text-error-700">{error}</p>
        </div>
      )}

      {preview && (
        <button
          onClick={handleSubmit}
          disabled={uploadMutation.isPending || !name.trim()}
          className="btn-primary w-full animate-slide-up py-3 text-base"
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

      {uploadMutation.isPending && (
        <p className="text-center text-xs text-gray-500">Detecting face · Cropping · Optimizing…</p>
      )}
    </div>
  )
}
