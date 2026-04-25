<template>
  <el-container class="layout-wrapper">
    <!-- Sidebar -->
    <el-aside width="220px" class="sidebar">
      <div class="brand">DLP CONSOLE</div>

      <el-menu
        :default-active="activeMenu"
        class="el-menu-vertical"
        router
        background-color="#304156"
        text-color="#bfcbd9"
        active-text-color="#409EFF"
      >
        <el-menu-item index="/dashboard">
          <el-icon><HomeFilled /></el-icon>
          <span>Trang ch?</span>
        </el-menu-item>

        <el-menu-item index="/agents">
          <el-icon><Monitor /></el-icon>
          <span>Qu?n l? Agent</span>
        </el-menu-item>

        <el-menu-item index="/policies">
          <el-icon><Lock /></el-icon>
          <span>Chính sách DLP</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <!-- Header -->
      <el-header class="header">
        <div class="header-left">Qu?n tr? h? th?ng</div>
        <div class="header-right">
          <el-button type="info" text @click="handleLogout"
            >Ðãng xu?t</el-button
          >
        </div>
      </el-header>

      <!-- N?i dung chính -->
      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { HomeFilled, Monitor, Lock } from "@element-plus/icons-vue";

const route = useRoute();
const router = useRouter();

const activeMenu = computed(() => route.path);

const handleLogout = () => {
  localStorage.removeItem("token");
  router.push("/login");
};
</script>

<style scoped>
.layout-wrapper {
  height: 100vh;
}
.sidebar {
  background-color: #304156;
  color: #fff;
}
.brand {
  height: 60px;
  line-height: 60px;
  text-align: center;
  font-weight: bold;
  font-size: 18px;
  background-color: #2b3649;
}
.header {
  background-color: #fff;
  border-bottom: 1px solid #e6e6e6;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.main-content {
  background-color: #f0f2f5;
}
.el-menu {
  border-right: none;
}
</style>
