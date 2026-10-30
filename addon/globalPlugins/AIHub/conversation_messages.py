"""Build the provider-neutral message list (text, files, audio) sent with a request."""

import base64
import mimetypes
import os
import tempfile
import addonHandler
from .consts import TEMP_DIR, ensure_temp_dir, ContentType, Role, AUDIO_EXT_TO_FORMAT
from .imagehelper import encode_image, resize_image
from .image_file import AttachmentFileTypes

addonHandler.initTranslation()


class ConversationMessagesMixin:
	"""Mixin for ConversationDialog."""

	def getFilesContent(
		self,
		filesList: list = None,
		prompt: str = None
	) -> list:
		"""Build OpenAI-style content parts (image_url / input_file) for the given attachments."""
		conf = self.conf
		if not filesList:
			filesList = self.filesList
		parts = []
		if prompt:
			parts.append({
				"type": ContentType.TEXT,
				"text": prompt
			})
		model = self.getCurrentModel() if hasattr(self, "getCurrentModel") else None
		provider = getattr(model, "provider", "") if model else ""
		for attachment in filesList:
			path = attachment.path
			if attachment.type == AttachmentFileTypes.IMAGE_URL:
				parts.append({"type": ContentType.IMAGE_URL, "image_url": {"url": path}})
			elif attachment.type == AttachmentFileTypes.DOCUMENT_URL:
				parts.append({"type": ContentType.INPUT_FILE, "file_url": path, "filename": attachment.name})
			elif attachment.type == AttachmentFileTypes.IMAGE_LOCAL:
				if conf["images"]["resize"]:
					ensure_temp_dir()
					fd, path_resized_image = tempfile.mkstemp(suffix=".jpg", dir=TEMP_DIR)
					os.close(fd)
					if resize_image(
						path,
						max_width=conf["images"]["maxWidth"],
						max_height=conf["images"]["maxHeight"],
						quality=conf["images"]["quality"],
						target=path_resized_image
					):
						path = path_resized_image
				base64_image = encode_image(path)
				mime_type, _ = mimetypes.guess_type(path)
				parts.append({
					"type": ContentType.IMAGE_URL,
					"image_url": {
						"url": f"data:{mime_type};base64,{base64_image}"
					}
				})
			elif attachment.type == AttachmentFileTypes.DOCUMENT_LOCAL:
				part = {
					"type": ContentType.INPUT_FILE,
					"file_path": path,
					"filename": attachment.name,
				}
				if provider:
					from .promptcache import stored_file_id

					fid = stored_file_id(attachment, provider)
					if fid:
						part["file_id"] = fid
				parts.append(part)
			else:
				raise ValueError(f"Invalid attachment type for {path}")
		return parts

	def getAudioContent(self, audioPaths=None, prompt=None):
		"""Build input_audio content for audio-capable models."""
		audioPaths = audioPaths or self.audioPathList
		if not audioPaths:
			return []
		content = []
		if prompt:
			content.append({"type": ContentType.TEXT, "text": prompt})
		for path in audioPaths:
			path_str = path if isinstance(path, str) else getattr(path, "path", str(path))
			if not os.path.exists(path_str):
				continue
			ext = os.path.splitext(path_str)[1].lower()
			fmt = AUDIO_EXT_TO_FORMAT.get(ext, "wav")
			with open(path_str, "rb") as f:
				data_b64 = base64.b64encode(f.read()).decode("utf-8")
			content.append({
				"type": ContentType.INPUT_AUDIO,
				"input_audio": {"data": data_b64, "format": fmt}
			})
		return content

	def getMessages(
		self,
		messages: list,
		*,
		until_block=None,
	):
		"""Append chat history blocks to ``messages``.

		When ``until_block`` is set, include every prior turn (user + assistant) and
		only the user turn for ``until_block`` (no assistant reply), then stop.
		"""
		block = self.firstBlock
		while block:
			userContent = []
			if block.filesList or getattr(block, "audioPathList", None):
				if block.prompt:
					userContent.append({"type": ContentType.TEXT, "text": block.prompt})
				if block.filesList:
					userContent.extend(self.getFilesContent(block.filesList, prompt=None))
				if getattr(block, "audioPathList", None):
					tlist = getattr(block, "audioTranscriptList", None)
					if tlist is not None and len(tlist) == len(block.audioPathList) and any(t for t in tlist):
						for t in tlist:
							if t:
								userContent.append({"type": ContentType.TEXT, "text": t})
					else:
						userContent.extend(self.getAudioContent(block.audioPathList, prompt=None))
			elif block.prompt:
				userContent = block.prompt
			if userContent:
				messages.append({
					"role": Role.USER,
					"content": userContent
				})
			stop_after = until_block is not None and block is until_block
			if not stop_after and block.responseText:
				messages.append({
					"role": Role.ASSISTANT,
					"content": block.responseText
				})
			if stop_after:
				break
			block = block.next
