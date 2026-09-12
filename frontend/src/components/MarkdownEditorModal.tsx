import { App as AntApp, Button, Input, Modal, Space, Spin } from "antd";
import { useCallback, useEffect, useState } from "react";
import { getFileContent, updateFileContent } from "../lib/api";
import { MarkdownRenderer } from "./MarkdownRenderer";
import type { FileDetail } from "../types";

interface MarkdownEditorModalProps {
  file: FileDetail | null;
  open: boolean;
  onClose: () => void;
  /** 保存成功后回调，让上层刷新文件列表/预览 */
  onSaved?: () => void;
}

export function MarkdownEditorModal({
  file,
  open,
  onClose,
  onSaved
}: MarkdownEditorModalProps) {
  const { message } = AntApp.useApp();
  const [content, setContent] = useState("");
  const [original, setOriginal] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    if (!file) {
      return;
    }
    setLoading(true);
    try {
      const response = await getFileContent(file.id);
      setContent(response.content);
      setOriginal(response.content);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "读取文件内容失败");
    } finally {
      setLoading(false);
    }
  }, [file, message]);

  useEffect(() => {
    if (open && file) {
      void load();
    }
  }, [file, load, open]);

  async function handleSave() {
    if (!file) {
      return;
    }
    setSaving(true);
    try {
      await updateFileContent(file.id, content);
      setOriginal(content);
      message.success("已保存");
      onSaved?.();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  const dirty = content !== original;

  return (
    <Modal
      cancelText="关闭"
      className="markdown-editor-modal"
      confirmLoading={saving}
      footer={
        <Space>
          <Button onClick={onClose}>关闭</Button>
          <Button
            disabled={!dirty}
            onClick={() => setContent(original)}
          >
            撤销改动
          </Button>
          <Button
            disabled={!dirty}
            loading={saving}
            onClick={() => void handleSave()}
            type="primary"
          >
            保存
          </Button>
        </Space>
      }
      maskClosable={false}
      onCancel={onClose}
      open={open}
      title={`编辑 ${file?.name || ""}`}
      width={1100}
    >
      {loading ? (
        <div className="editor-loading">
          <Spin />
        </div>
      ) : (
        <div className="editor-split">
          <Input.TextArea
            className="editor-textarea"
            onChange={(event) => setContent(event.target.value)}
            placeholder="在此编辑 Markdown 正文"
            value={content}
          />
          <div className="editor-preview markdown-body">
            <MarkdownRenderer content={content} />
          </div>
        </div>
      )}
    </Modal>
  );
}
