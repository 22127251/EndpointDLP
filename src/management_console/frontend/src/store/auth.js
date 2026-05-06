import { defineStore } from "pinia";
import apiClient from "@/api/axios";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    token: localStorage.getItem("access_token") || null,
  }),
  actions: {
    async login(username, password) {
      try {
        const response = await apiClient.post("/auth/login", {
          username,
          password,
        });
        this.token = response.data.access_token;
        localStorage.setItem("access_token", this.token);
        return true;
      } catch (error) {
        throw error;
      }
    },
    logout() {
      this.token = null;
      localStorage.removeItem("access_token");
    },
  },
});
