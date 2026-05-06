import axios from "axios";
import { ElMessage } from "element-plus";

const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1",
  timeout: 10000,
});

apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem("access_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response ? error.response.status : null;

    if (status === 401) {
      localStorage.removeItem("access_token");
      window.location.href = "/login";
    } else if (status === 403) {
      ElMessage.error("Permission denied");
    } else if (status >= 500) {
      ElMessage.error("Server error. Please try again later.");
    }

    return Promise.reject(error);
  },
);

export default apiClient;
