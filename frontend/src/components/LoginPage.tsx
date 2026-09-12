import { LockOutlined, SafetyCertificateOutlined, UserOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Form, Input, Tabs } from "antd";
import { useState } from "react";
import { useAuth } from "../hooks/useAuth";

interface LoginFormValues {
  username: string;
  password: string;
  display_name?: string;
}

export function LoginPage() {
  const { message } = AntApp.useApp();
  const { login, register } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<LoginFormValues>();

  async function handleFinish(values: LoginFormValues) {
    setSubmitting(true);
    try {
      if (mode === "login") {
        await login({ username: values.username, password: values.password });
        message.success("登录成功");
      } else {
        await register({
          username: values.username,
          password: values.password,
          display_name: values.display_name || undefined
        });
        message.success("注册成功，已自动登录");
      }
    } catch (error) {
      // 注册接口可能在服务端被 ALLOW_SELF_REGISTER=0 关掉，错误原样透传给用户
      message.error(error instanceof Error ? error.message : "操作失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        <div className="login-brand">
          <span className="panel-kicker">DEEPSEARCH</span>
          <h1>深度研搜</h1>
          <p>对话式多智能体研究台</p>
        </div>

        <Tabs
          activeKey={mode}
          className="login-tabs"
          items={[
            { key: "login", label: "登录" },
            { key: "register", label: "注册" }
          ]}
          onChange={(key) => {
            setMode(key as "login" | "register");
            form.resetFields(["display_name"]);
          }}
        />

        <Form
          form={form}
          layout="vertical"
          onFinish={handleFinish}
          requiredMark={false}
          autoComplete="off"
        >
          <Form.Item
            label="用户名"
            name="username"
            rules={[
              { required: true, message: "请输入用户名" },
              { min: 3, max: 64, message: "用户名长度需在 3~64 个字符之间" }
            ]}
          >
            <Input
              autoFocus
              prefix={<UserOutlined />}
              placeholder="3~64 位，小写字母、数字、下划线或点"
              size="large"
            />
          </Form.Item>

          {mode === "register" ? (
            <Form.Item label="显示名（可选）" name="display_name">
              <Input prefix={<SafetyCertificateOutlined />} placeholder="同事看到的名字" size="large" />
            </Form.Item>
          ) : null}

          <Form.Item
            label="密码"
            name="password"
            rules={[
              { required: true, message: "请输入密码" },
              { min: 6, max: 128, message: "密码长度需在 6~128 个字符之间" }
            ]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="请输入密码"
              size="large"
            />
          </Form.Item>

          <Button
            block
            className="login-submit"
            htmlType="submit"
            loading={submitting}
            size="large"
            type="primary"
          >
            {mode === "login" ? "登录" : "注册并登录"}
          </Button>
        </Form>
      </div>
    </div>
  );
}
