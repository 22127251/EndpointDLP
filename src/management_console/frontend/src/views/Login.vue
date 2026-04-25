<template>
  <div class="login-container">
    <el-card class="login-card">
      <template #header>
        <h2 style="text-align: center; margin: 0">DLP Management System</h2>
      </template>

      <el-form :model="loginForm" label-position="top">
        <el-form-item label="Tài kho?n">
          <el-input v-model="loginForm.username" placeholder="Nh?p username" />
        </el-form-item>

        <el-form-item label="M?t kh?u">
          <el-input
            v-model="loginForm.password"
            type="password"
            placeholder="Nh?p password"
            show-password
          />
        </el-form-item>

        <el-button
          type="primary"
          style="width: 100%"
          @click="handleLogin"
          :loading="loading"
        >
          Ðãng nh?p
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";

const router = useRouter();
const loading = ref(false);
const loginForm = reactive({
  username: "",
  password: "",
});

const handleLogin = () => {
  if (!loginForm.username || !loginForm.password) {
    return ElMessage.error("Please fill in all required fields!");
  }

  loading.value = true;

  setTimeout(() => {
    loading.value = false;
    if (loginForm.username === "admin" && loginForm.password === "123456") {
      localStorage.setItem("token", "fake-jwt-token");
      ElMessage.success("Login successful!");
      router.push("/");
    } else {
      ElMessage.error("Invalid username or password!");
    }
  }, 1000);
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
}
</style>
