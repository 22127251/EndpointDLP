import { defineStore } from "pinia";
import apiClient from "@/api/axios";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    token: localStorage.getItem("access_token") || null,
    user: JSON.parse(localStorage.getItem("user_info")) || null,
  }),
  getters: {
    isAdmin: (state) => state.user?.role === "admin",
    currentUserId: (state) => state.user?.id,
    userDisplayName: (state) =>
      state.user?.full_name || state.user?.username || "User",
    userRoleLabel: (state) => state.user?.role || "viewer",
  },
  actions: {
    setUser(userData) {
      this.user = userData;
      localStorage.setItem("user_info", JSON.stringify(userData));
    },
    async login(username, password) {
      try {
        const response = await apiClient.post("/auth/login", {
          username,
          password,
        });
        this.token = response.data.access_token;
        localStorage.setItem("access_token", this.token);

        this.user = response.data.user_info;
        localStorage.setItem("user_info", JSON.stringify(this.user));
        return true;
      } catch (error) {
        throw error;
      }
    },
    logout() {
      this.token = null;
      localStorage.removeItem("access_token");
      this.user = null;
      localStorage.removeItem("user_info");
    },
  },
});
