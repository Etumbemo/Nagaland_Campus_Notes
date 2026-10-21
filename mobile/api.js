import * as SecureStore from 'expo-secure-store';
import { File, UploadType } from 'expo-file-system';

const API_URL = process.env.EXPO_PUBLIC_API_URL?.replace(/\/$/, '');
const TOKEN_KEY = 'campus_notes_access_token';

function normalizeKeys(value) {
  if (Array.isArray(value)) return value.map(normalizeKeys);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => {
        const normalizedKey = key.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
        let normalizedValue = normalizeKeys(item);
        if (key === 'file_url' && typeof item === 'string' && API_URL) {
          const fileUrl = new URL(item, API_URL);
          if (fileUrl.pathname.startsWith('/uploads/')) {
            normalizedValue = `${API_URL}${fileUrl.pathname}${fileUrl.search}${fileUrl.hash}`;
          }
        }
        return [normalizedKey, normalizedValue];
      }),
    );
  }
  return value;
}

export async function request(path, options = {}) {
  if (!API_URL) {
    throw new Error('Set EXPO_PUBLIC_API_URL in mobile/.env and restart Expo.');
  }

  const token = await SecureStore.getItemAsync(TOKEN_KEY);
  const headers = { ...options.headers };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }

  const response = await fetch(`${API_URL}${path}`, { ...options, headers });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg).join(', ')
      : detail || data?.error || 'Request failed';
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return normalizeKeys(data);
}

export function getAiPreparationOptions() {
  return request('/api/ai-prep/options');
}

export function generateAiPreparation(payload) {
  return request('/api/ai-prep/generate', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function signIn(email, password, expectedAccountType) {
  const result = await request('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
  if (!['user', 'institute'].includes(result.user.role)) {
    throw new Error('Administrator accounts must sign in at the backend admin dashboard.');
  }
  if (expectedAccountType && result.user.role !== expectedAccountType) {
    const actualType = result.user.role === 'institute' ? 'Institute staff' : 'Reader';
    throw new Error(`This email is registered as ${actualType}. Choose ${actualType} to sign in.`);
  }
  await SecureStore.setItemAsync(TOKEN_KEY, result.session.accessToken);
  return result.user;
}

export async function requestPasswordReset(email) {
  return request('/api/auth/password-reset/request', {
    method: 'POST',
    body: JSON.stringify({ email }),
  });
}

export async function confirmPasswordReset(email, otp, newPassword) {
  return request('/api/auth/password-reset/confirm', {
    method: 'POST',
    body: JSON.stringify({ email, otp, new_password: newPassword }),
  });
}

export async function register(
  name,
  email,
  password,
  accountType = 'user',
  instituteName = '',
  instituteDetails = {},
) {
  let result;
  if (accountType === 'institute') {
    if (!instituteDetails.supportingDocument) {
      throw new Error('Upload a supporting document to register institute staff.');
    }
    if (!API_URL) {
      throw new Error('Set EXPO_PUBLIC_API_URL in mobile/.env and restart Expo.');
    }

    const document = instituteDetails.supportingDocument;
    const response = await new File(document.uri).upload(`${API_URL}/api/auth/register/institute`, {
      httpMethod: 'POST',
      uploadType: UploadType.MULTIPART,
      fieldName: 'file',
      mimeType: document.mimeType || 'application/octet-stream',
      parameters: {
        name,
        email,
        password,
        phone: instituteDetails.phone,
        designation: instituteDetails.designation,
        institute_name: instituteName,
      },
    });

    try {
      result = JSON.parse(response.body);
    } catch {
      result = null;
    }
    if (response.status < 200 || response.status >= 300) {
      const detail = result?.detail;
      const message = Array.isArray(detail)
        ? detail.map((item) => item.msg).join(', ')
        : detail || result?.error || `Institute registration failed (HTTP ${response.status})`;
      throw new Error(message);
    }
    if (!result) throw new Error('The server returned an invalid response after registration.');
  } else {
    result = await request('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify({
        name,
        email,
        password,
      }),
    });
  }
  await SecureStore.setItemAsync(TOKEN_KEY, result.session.accessToken);
  return result.user;
}

export async function signOut() {
  try {
    await request('/api/auth/logout', { method: 'POST' });
  } finally {
    await SecureStore.deleteItemAsync(TOKEN_KEY);
  }
}

export async function getStoredUser() {
  const token = await SecureStore.getItemAsync(TOKEN_KEY);
  if (!token) return null;
  try {
    const user = (await request('/api/auth/me')).user;
    if (!['user', 'institute'].includes(user.role)) {
      await SecureStore.deleteItemAsync(TOKEN_KEY);
      return null;
    }
    return user;
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      await SecureStore.deleteItemAsync(TOKEN_KEY);
    }
    throw error;
  }
}

export async function uploadPdf(file) {
  if (!API_URL) {
    throw new Error('Set EXPO_PUBLIC_API_URL in mobile/.env and restart Expo.');
  }

  const token = await SecureStore.getItemAsync(TOKEN_KEY);
  const response = await new File(file.uri).upload(`${API_URL}/api/uploads`, {
    httpMethod: 'POST',
    uploadType: UploadType.MULTIPART,
    fieldName: 'file',
    mimeType: file.mimeType || 'application/pdf',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });

  let data;
  try {
    data = JSON.parse(response.body);
  } catch {
    data = null;
  }
  if (response.status < 200 || response.status >= 300) {
    const detail = data?.detail;
    const message = Array.isArray(detail)
      ? detail.map((item) => item.msg).join(', ')
      : detail || data?.error || `PDF upload failed (HTTP ${response.status})`;
    throw new Error(message);
  }
  if (!data) throw new Error('The server returned an invalid response after uploading the PDF.');
  return normalizeKeys(data);
}
