<template>
  <div class="login-container">
    <el-card class="login-card">
      <h2>DLP MANAGEMENT</h2>
      <el-form :model="loginForm" @keyup.enter="handleLogin">
        <el-form-item>
          <el-input v-model="loginForm.username" placeholder="Username" />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="loginForm.password"
            type="password"
            placeholder="Password"
            show-password
          />
        </el-form-item>
        <el-button
          type="primary"
          :loading="loading"
          @click="handleLogin"
          style="width: 100%"
        >
          Login
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { useAuthStore } from "@/store/auth";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

const auth = useAuthStore();
const router = useRouter();
const loading = ref(false);
const loginForm = ref({ username: "", password: "" });

const handleLogin = async () => {
  if (!loginForm.value.username || !loginForm.value.password) {
    return ElMessage.warning("Invalid username or password");
  }
  loading.value = true;
  try {
    const data = await auth.login(
      loginForm.value.username,
      loginForm.value.password,
    );
    ElMessage.success(`Welcome, ${auth.userDisplayName}!`);
    router.push("/");
  } catch (error) {
    ElMessage.error("Invalid username or password");
    console.error("Login error:", error);
  } finally {
    loading.value = false;
  }
};
</script>

<style scoped>
.login-container {
  height: 100vh;
  display: flex;
  justify-content: center;
  align-items: center;
  background-color: #f5f7fa;
}
.login-card {
  width: 400px;
  padding: 20px;
  text-align: center;
}
</style>
