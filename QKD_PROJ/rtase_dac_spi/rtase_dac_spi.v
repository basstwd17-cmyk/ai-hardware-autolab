// RTASE_V1 channel-A DAC controller with writable CODE0..CODE3 registers.
//
// Configuration path (not real-time):
//   BBB Linux SPI0 -> 4 x 14-bit registers with readback
// Real-time path:
//   BBB PRU SEL[1:0] -> code mux -> D1B[13:0]
//   BBB PRU UPDATE   -> D1CLK
//
// SPI mode 0, MSB first, one 24-bit transaction while CS is low:
//   byte 0: bit7 R/W (0 write, 1 read), bits1:0 register address, bits6:2 zero
//   byte 1: bits5:0 are code[13:8] (top two bits ignored/returned zero)
//   byte 2: code[7:0]
// A read sends [0x80 | address, 0, 0] and receives [0, code_hi, code_lo].

module rtase_dac_spi (
    input  wire        sym_a0,
    input  wire        sym_a1,
    input  wire        ldac_a,
    input  wire        spi_sclk,
    input  wire        spi_mosi,
    input  wire        spi_cs_n,
    output wire        spi_miso,
    output reg  [13:0] d1b,
    output wire        d1clk
);
    reg [13:0] code0;
    reg [13:0] code1;
    reg [13:0] code2;
    reg [13:0] code3;

    reg [23:0] rx_shift;
    reg [7:0]  command;
    reg [15:0] read_word;
    reg [4:0]  bit_count;

    // MachXO2 configuration initializes these registers at power-up/SRAM load.
    initial begin
        code0 = 14'h0000;
        code1 = 14'h1555;
        code2 = 14'h2AAA;
        code3 = 14'h3FFF;
        rx_shift = 24'h0;
        command = 8'h0;
        read_word = 16'h0;
        bit_count = 5'h0;
    end

    always @* begin
        case ({sym_a1, sym_a0})
            2'b00: d1b = code0;
            2'b01: d1b = code1;
            2'b10: d1b = code2;
            default: d1b = code3;
        endcase
    end

    assign d1clk = ldac_a;

    // Mode-0 input sampling on SCLK rising edges.  Each spidev operation is
    // exactly one 24-clock frame, so the counter wraps at the frame boundary.
    // Do not use CS as an asynchronous reset: MachXO2 synthesis can promote
    // it to the device-wide GSR network, which holds this SPI slave in reset
    // for the entire active-low transaction.
    always @(posedge spi_sclk) begin
        if (!spi_cs_n) begin
            rx_shift <= {rx_shift[22:0], spi_mosi};
            if (bit_count == 5'd7) begin
                command <= {rx_shift[6:0], spi_mosi};
                case ({rx_shift[0], spi_mosi})
                    2'b00: read_word <= {2'b00, code0};
                    2'b01: read_word <= {2'b00, code1};
                    2'b10: read_word <= {2'b00, code2};
                    default: read_word <= {2'b00, code3};
                endcase
            end
            if (bit_count == 5'd23) begin
                // Only commands 0x00..0x03 write.  Reserved command bits must
                // be zero, preventing malformed/read frames from altering data.
                if (!rx_shift[22] && rx_shift[21:17] == 5'b00000) begin
                    case (rx_shift[16:15])
                        2'b00: code0 <= {rx_shift[12:0], spi_mosi};
                        2'b01: code1 <= {rx_shift[12:0], spi_mosi};
                        2'b10: code2 <= {rx_shift[12:0], spi_mosi};
                        default: code3 <= {rx_shift[12:0], spi_mosi};
                    endcase
                end
                bit_count <= 5'h0;
            end else begin
                bit_count <= bit_count + 1'b1;
            end
        end
    end

    // During the command byte MISO is zero.  Once eight command bits have
    // been captured, the selected 16-bit read word is presented MSB first.
    // The bit index changes immediately after each rising sample edge and is
    // therefore stable for the next complete mode-0 clock period.
    assign spi_miso = (!spi_cs_n && bit_count >= 5'd8 && bit_count <= 5'd23)
                    ? read_word[23 - bit_count]
                    : 1'b0;
endmodule
