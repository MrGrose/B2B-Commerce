load_operational_image_tag() {
  local root="${1:-.}"
  if [[ -n "${B2B_COMMERCE_IMAGE_TAG:-}" ]]; then
    export B2B_COMMERCE_IMAGE_TAG
    return 0
  fi
  local tag_file="${root}/.deploy-tag"
  if [[ -f "$tag_file" ]]; then
    B2B_COMMERCE_IMAGE_TAG="$(tr -d '[:space:]' <"$tag_file")"
    if [[ -n "$B2B_COMMERCE_IMAGE_TAG" ]]; then
      export B2B_COMMERCE_IMAGE_TAG
      return 0
    fi
  fi
  echo "ERROR: задайте B2B_COMMERCE_IMAGE_TAG или выполните deploy (создаст .deploy-tag)" >&2
  return 1
}
