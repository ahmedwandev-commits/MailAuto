/* My picture: the chosen photo is cut to a centred square of 512 px and sent as a small JPEG (the server still checks it).
   Without this script the file itself is sent. No inline script (strict CSP); blob: addresses are not allowed, so data:. */
(function () {
  "use strict";
  const file = document.getElementById("picFile");
  const data = document.getElementById("picData");
  const box = document.getElementById("picPreview");
  const img = document.getElementById("picPreviewImg");
  if (!file || !data) return;
  const SIZE = 512;

  file.addEventListener("change", () => {
    data.value = "";
    box.hidden = true;
    const f = file.files && file.files[0];
    if (!f || !/^image\/(jpeg|png|webp)$/.test(f.type)) return;
    const reader = new FileReader();
    reader.onload = () => {
      const pic = new Image();
      pic.onload = () => {
        const side = Math.min(pic.naturalWidth, pic.naturalHeight);
        if (!side) return;
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = Math.min(SIZE, side);
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#ffffff";                 // a transparent PNG gets a white background
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(pic, (pic.naturalWidth - side) / 2, (pic.naturalHeight - side) / 2, side, side,
          0, 0, canvas.width, canvas.height);
        const url = canvas.toDataURL("image/jpeg", 0.88);
        data.value = url;
        img.src = url;
        box.hidden = false;
      };
      pic.src = reader.result;
    };
    reader.readAsDataURL(f);
  });

  // the square photo goes in place of the file (smaller, and the same for every phone camera)
  document.getElementById("picForm").addEventListener("submit", () => {
    if (data.value) file.disabled = true;
  });
})();
