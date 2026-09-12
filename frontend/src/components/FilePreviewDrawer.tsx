import { DownloadOutlined, EditOutlined, ReloadOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Drawer, Empty, Skeleton, Space } from "antd";
import { useCallback, useEffect, useState } from "react";
import { getDownloadUrl, previewFile } from "../lib/api";
import { MarkdownRenderer } from "./MarkdownRenderer";
import type { FileDetail, PreviewResponse } from "../types";

interface FilePreviewDrawerProps {
  file: FileDetail | null;
  open: boolean;
  onClose: () => void;
  onEdit: (file: FileDetail) => void;
}

// 与后端 app/api/routes/files.py 中的常量保持一致：
// _EDITABLE_EXTS 决定能否在线编辑，_INLINE_TEXT_EXTS 决定预览是直接给正文还是给地址。
const EDITABLE_EXTS = [".md", ".txt"];
const INLINE_TEXT_EXTS = [".md", ".txt", ".csv", ".json", ".log"];

function hasExt(name: string, exts: string[]): boolean {
  const lower = name.toLowerCase();
  return exts.some((ext) => lower.endsWith(ext));
}

function isMarkdown(name: string): boolean {
  return hasExt(name, [".md"]);
}

function isImage(mime: string | null, name: string): boolean {
  if (mime?.startsWith("image/")) {
    return true;
  }
  return /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(name);
}

function isPdf(mime: string | null, name: string): boolean {
  return mime === "application/pdf" || name.toLowerCase().endsWith(".pdf");
}

export function FilePreviewDrawer({ file, open, onClose, onEdit }: FilePreviewDrawerProps) {
  const { message } = AntApp.useApp();
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!file) {
      return;
    }
    setLoading(true);
    setPreview(null);
    try {
      setPreview(await previewFile(file.id));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "预览失败");
    } finally {
      setLoading(false);
    }
  }, [file, message]);

  useEffect(() => {
    if (open && file) {
      void load();
    }
    if (!open) {
      setPreview(null);
    }
  }, [file, load, open]);

  async function handleDownload() {
    if (!file) {
      return;
    }
    try {
      const response = await getDownloadUrl(file.id);
      // 预签名地址由对象存储直出，用它开新标签即可
      window.open(response.url, "_blank", "noopener,noreferrer");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "获取下载地址失败");
    }
  }

  const canEdit = file ? hasExt(file.name, EDITABLE_EXTS) : false;
  // 后端对超过 512KB 的文本也会退化成给地址，这里同步判断，避免拿到 url 却没有渲染分支
  const inlineExpected = file ? hasExt(file.name, INLINE_TEXT_EXTS) : false;

  return (
    <Drawer
      className="file-preview-drawer"
      destroyOnHidden
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => void load()} size="small">
            刷新
          </Button>
          {canEdit ? (
            <Button
              icon={<EditOutlined />}
              onClick={() => file && onEdit(file)}
              size="small"
              type="primary"
            >
              编辑
            </Button>
          ) : null}
          <Button
            icon={<DownloadOutlined />}
            onClick={() => void handleDownload()}
            size="small"
          >
            下载
          </Button>
        </Space>
      }
      onClose={onClose}
      open={open}
      title={file?.name || "文件预览"}
      width={760}
    >
      {loading ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : !preview ? (
        <Empty description="没有可预览的内容" />
      ) : preview.mode === "inline" ? (
        file && isMarkdown(file.name) ? (
          <div className="markdown-body">
            <MarkdownRenderer content={preview.content || ""} />
          </div>
        ) : (
          <pre className="preview-text">{preview.content}</pre>
        )
      ) : file && isImage(preview.mime_type, file.name) ? (
        <img alt={preview.name} className="preview-image" src={preview.url || ""} />
      ) : inlineExpected && file && file.size > 512 * 1024 ? (
        // 文本类但超过后端内联上限，只能给预签名地址
        <iframe className="preview-frame" src={preview.url || ""} title={preview.name} />
      ) : isPdf(preview.mime_type, preview.name) ? (
        <iframe className="preview-frame" src={preview.url || ""} title={preview.name} />
      ) : (
        <div className="preview-fallback">
          <Empty description="该格式不支持在线预览" />
          <Button onClick={() => void handleDownload()} type="primary">
            下载后查看
          </Button>
        </div>
      )}
    </Drawer>
  );
}
