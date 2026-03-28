const API_URL = 'https://stock-backend-5ljo.onrender.com/stocks';

async function runStrategy() {
  const tbody = document.getElementById('resultBody');
  const status = document.getElementById('status');

  status.textContent = '資料讀取中...';
  tbody.innerHTML = `<tr><td colspan="7">讀取中...</td></tr>`;

  try {
    const response = await fetch(API_URL);
    if (!response.ok) throw new Error('伺服器回應錯誤');

    const stocks = await response.json();

    if (stocks.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7">沒有符合條件的股票</td></tr>`;
      status.textContent = '讀取完成';
      return;
    }

    tbody.innerHTML = stocks.map(stock => `
      <tr>
        <td>${stock.code}</td>
        <td>${stock.name}</td>
        <td>${stock.close}</td>
        <td>${stock.ma5}</td>
        <td>${stock.ma20}</td>
        <td>${stock.ma60}</td>
        <td>${stock.volume}</td>
      </tr>
    `).join('');

    status.textContent = `讀取完成，共 ${stocks.length} 檔`;
  } catch (error) {
    tbody.innerHTML = `<tr><td colspan="7">讀取失敗</td></tr>`;
    status.textContent = `錯誤：${error.message}`;
  }
}

document.getElementById('runBtn').addEventListener('click', runStrategy);
